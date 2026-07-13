pub mod model;

use model::{
    bbox_area, center_distance, denormalize_bbox, expand_bbox, iou, normalize_bbox, union_bbox,
    BBox, ClassifiedRegions, FrameObservation, FlushPolicy, GameProfile, LayoutCandidate,
    LayoutKind, LayoutPrediction, LayoutState, PredictedRegion, ProfileOpenOptions, RegionClass,
    RegionCluster, RegionObservation, RoleHintCounts,
};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

pub type Result<T> = std::result::Result<T, ProfilerError>;

#[derive(Debug)]
pub enum ProfilerError {
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl std::fmt::Display for ProfilerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProfilerError::Io(error) => write!(f, "io error: {error}"),
            ProfilerError::Json(error) => write!(f, "json error: {error}"),
        }
    }
}

impl std::error::Error for ProfilerError {}

impl From<std::io::Error> for ProfilerError {
    fn from(error: std::io::Error) -> Self {
        ProfilerError::Io(error)
    }
}

impl From<serde_json::Error> for ProfilerError {
    fn from(error: serde_json::Error) -> Self {
        ProfilerError::Json(error)
    }
}

pub struct DialogueLayoutProfiler {
    options: ProfileOpenOptions,
    profile_path: PathBuf,
    profile: GameProfile,
    observations_since_flush: u64,
    dirty: bool,
}

const OUTPUT_RECENT_FRAME_WINDOW: u64 = 8;

#[derive(Debug, Clone)]
struct ActiveCluster {
    id: String,
    bbox: BBox,
    class: RegionClass,
}

#[derive(Debug, Clone)]
struct FrameAnalysis {
    layout_kind: LayoutKind,
    active_clusters: Vec<ActiveCluster>,
    focus_bbox: Option<BBox>,
    occluded_base_layout_id: Option<String>,
    events: Vec<String>,
}

impl DialogueLayoutProfiler {
    pub fn open(options: ProfileOpenOptions) -> Result<Self> {
        let profile_path = PathBuf::from(&options.profile_path);
        let profile = load_or_create_profile(&profile_path, &options.profile_id)?;
        Ok(Self {
            options,
            profile_path,
            profile,
            observations_since_flush: 0,
            dirty: false,
        })
    }

    pub fn observe_frame(&mut self, frame: FrameObservation) -> Result<LayoutPrediction> {
        let mut events = Vec::new();
        self.profile.frame_count += 1;
        let frame_index = self.profile.frame_count;
        self.profile.last_frame_id = frame.frame_id.clone();
        self.profile.last_timestamp_ms = frame.timestamp_ms;
        self.profile.last_frame_width = Some(frame.frame.width);
        self.profile.last_frame_height = Some(frame.frame.height);
        self.decay_recent_support(frame_index);

        let usable_text = frame
            .regions
            .iter()
            .filter(|region| region.confidence >= self.options.config.min_region_confidence)
            .collect::<Vec<_>>();

        if usable_text.is_empty() {
            self.profile.empty_frame_streak += 1;
            events.push("no_usable_text_regions".to_string());
        } else {
            self.profile.empty_frame_streak = 0;
        }

        let mut active_indices = Vec::new();
        for region in usable_text.iter().copied() {
            let index = self.update_region_cluster(region, "text", frame.frame.width, frame.frame.height, &mut events);
            active_indices.push(index);
        }

        for region in &frame.ui_regions {
            let index = self.update_region_cluster(region, "ui", frame.frame.width, frame.frame.height, &mut events);
            active_indices.push(index);
        }
        active_indices.sort_unstable();
        active_indices.dedup();

        let mut active_clusters = Vec::new();
        for index in active_indices {
            let cluster = &mut self.profile.region_memories[index];
            let previous = cluster.classification.clone();
            classify_cluster(cluster);
            if cluster.classification != previous {
                events.push(format!(
                    "cluster_classified:{}:{:?}",
                    cluster.id, cluster.classification
                ));
            }
            active_clusters.push(ActiveCluster {
                id: cluster.id.clone(),
                bbox: cluster.mean_bbox,
                class: cluster.classification.clone(),
            });
        }

        let mut analysis = analyze_frame(
            &usable_text,
            &active_clusters,
            frame.frame.width,
            frame.frame.height,
            self.profile.layout_candidates.as_slice(),
        );
        events.append(&mut analysis.events);

        let active_layout_id = self.match_or_create_layout(&analysis, frame_index, &mut events);
        self.expire_tentative_layouts(frame_index, &mut events);

        let layout = active_layout_id
            .as_ref()
            .and_then(|id| self.profile.layout_candidates.iter().find(|layout| &layout.id == id))
            .cloned();

        let fallback_bbox = predict_current_frame_bbox(
            &usable_text,
            frame.frame.width,
            frame.frame.height,
            self.options.config.prediction_padding_ratio,
        );
        let fallback_bbox = if analysis.layout_kind == LayoutKind::Unknown {
            None
        } else {
            fallback_bbox
        };
        let predicted_bbox = prediction_bbox(
            layout.as_ref(),
            analysis.focus_bbox,
            fallback_bbox,
            self.options.config.prediction_padding_ratio,
        );

        let (mode, confidence) = prediction_mode_and_confidence(
            predicted_bbox,
            &usable_text,
            layout.as_ref(),
            self.profile.frame_count,
            &self.options.config,
        );

        let ocr_regions = predicted_bbox
            .map(|bbox| {
                events.push("prediction_from_learned_layout_or_current_frame".to_string());
                vec![PredictedRegion {
                    id: "ocr_primary".to_string(),
                    bbox: denormalize_bbox(bbox, frame.frame.width, frame.frame.height),
                    confidence,
                    purpose: Some(match analysis.layout_kind {
                        LayoutKind::Backlog => "backlog",
                        LayoutKind::OverlayPanel => "overlay_focus",
                        LayoutKind::ChoiceMenu => "choice",
                        _ => "dialogue",
                    }
                    .to_string()),
                    reason: Some(layout_kind_reason(&analysis.layout_kind).to_string()),
                }]
            })
            .unwrap_or_else(|| {
                events.push("fallback_triggered".to_string());
                Vec::new()
            });

        let (ignore_regions, speaker_regions, classified_regions) = if ocr_regions.is_empty() {
            (Vec::new(), Vec::new(), ClassifiedRegions::default())
        } else {
            let ignore_regions = self.predict_ignore_regions(frame.frame.width, frame.frame.height, &mut events);
            let speaker_regions = ignore_regions
                .iter()
                .filter(|region| region.reason.as_deref() == Some("stable_speaker_candidate"))
                .cloned()
                .collect::<Vec<_>>();
            let classified_regions =
                self.classified_regions(&ocr_regions, frame.frame.width, frame.frame.height);
            (ignore_regions, speaker_regions, classified_regions)
        };

        let should_checkpoint_established_layout = events
            .iter()
            .any(|event| event.starts_with("layout_established:"));

        let prediction = LayoutPrediction {
            frame_id: frame.frame_id.clone(),
            mode,
            active_layout_id,
            confidence,
            ocr_regions,
            ignore_regions,
            speaker_regions,
            classified_regions,
            debug: serde_json::json!({
                "layout_confidence": confidence,
                "layout_kind": format!("{:?}", analysis.layout_kind),
                "observed_region_count": frame.regions.len(),
                "usable_text_region_count": usable_text.len(),
                "cluster_count": self.profile.region_memories.len(),
                "layout_count": self.profile.layout_candidates.len(),
                "occluded_base_layout_id": analysis.occluded_base_layout_id,
                "events": events,
                "fallback_reason": if predicted_bbox.is_none() { Some("no reliable text regions") } else { None },
            }),
        };

        self.profile.last_prediction = Some(prediction.clone());
        self.observations_since_flush += 1;
        self.dirty = true;
        if self.should_flush() || should_checkpoint_established_layout {
            self.flush()?;
        }
        Ok(prediction)
    }

    pub fn flush(&mut self) -> Result<()> {
        if !self.dirty {
            return Ok(());
        }
        if let Some(parent) = self.profile_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp_path = tmp_path_for(&self.profile_path);
        let data = serde_json::to_vec_pretty(&self.profile)?;
        fs::write(&tmp_path, data)?;
        if let Err(error) = fs::rename(&tmp_path, &self.profile_path) {
            if self.profile_path.exists() {
                fs::remove_file(&self.profile_path)?;
                fs::rename(&tmp_path, &self.profile_path)?;
            } else {
                return Err(error.into());
            }
        }
        self.observations_since_flush = 0;
        self.dirty = false;
        Ok(())
    }

    pub fn profile(&self) -> &GameProfile {
        &self.profile
    }

    pub fn export_profile(&self) -> GameProfile {
        self.profile.clone()
    }

    fn should_flush(&self) -> bool {
        match self.options.flush_policy {
            FlushPolicy::EveryObservation => true,
            FlushPolicy::EveryNObservations { n } => self.observations_since_flush >= n.max(1),
            FlushPolicy::Manual => false,
        }
    }

    fn decay_recent_support(&mut self, frame_index: u64) {
        for cluster in &mut self.profile.region_memories {
            if cluster.last_seen_frame + 1 < frame_index {
                cluster.recent_count = cluster.recent_count.saturating_sub(1);
            }
        }
        for layout in &mut self.profile.layout_candidates {
            if layout.last_seen_frame + 1 < frame_index {
                layout.recent_support = layout.recent_support.saturating_sub(1);
                layout.confidence = (layout.confidence * 0.995).max(0.0);
            }
        }
    }

    fn update_region_cluster(
        &mut self,
        region: &RegionObservation,
        source: &str,
        width: u32,
        height: u32,
        events: &mut Vec<String>,
    ) -> usize {
        let bbox = normalize_bbox(region.bbox, width, height);
        let mut best_index = None;
        let mut best_score = 0.0;

        for (index, cluster) in self.profile.region_memories.iter().enumerate() {
            let overlap = iou(cluster.mean_bbox, bbox);
            let distance = center_distance(cluster.mean_bbox, bbox);
            let score = overlap.max((1.0 - (distance / self.options.config.cluster_match_center_distance)).max(0.0) * 0.6);
            if score > best_score {
                best_score = score;
                best_index = Some(index);
            }
        }

        let should_match = best_score >= self.options.config.cluster_match_iou;
        if !should_match {
            let id = format!("cluster_{}", self.profile.region_memories.len() + 1);
            let cluster = new_cluster(id.clone(), region, source, bbox, self.profile.frame_count);
            self.profile.region_memories.push(cluster);
            events.push(format!("cluster_created:{id}"));
            return self.profile.region_memories.len() - 1;
        }

        let index = best_index.expect("best index exists when should_match");
        let cluster = &mut self.profile.region_memories[index];
        update_cluster_stats(cluster, region, source, bbox, self.profile.frame_count);
        events.push(format!("cluster_updated:{}", cluster.id));
        index
    }

    fn match_or_create_layout(
        &mut self,
        analysis: &FrameAnalysis,
        frame_index: u64,
        events: &mut Vec<String>,
    ) -> Option<String> {
        if analysis.active_clusters.is_empty() {
            return None;
        }
        if analysis.layout_kind == LayoutKind::Unknown || analysis.focus_bbox.is_none() {
            return None;
        }

        let active_ids = layout_evidence_ids(analysis);

        let mut best_index = None;
        let mut best_score = 0.0;
        for (index, layout) in self.profile.layout_candidates.iter().enumerate() {
            if layout.state == LayoutState::Retired {
                continue;
            }
            if layout.kind != analysis.layout_kind && layout.kind != LayoutKind::Unknown {
                continue;
            }
            let score = layout_match_score(layout, analysis, &active_ids);
            if score > best_score {
                best_score = score;
                best_index = Some(index);
            }
        }

        if let Some(index) = best_index.filter(|_| best_score >= self.options.config.layout_match_overlap) {
            let layout = &mut self.profile.layout_candidates[index];
            layout.support_count += 1;
            layout.recent_support += 1;
            layout.last_seen_frame = frame_index;
            layout.confidence = (layout.confidence + 0.05 + best_score * 0.05).min(0.98);
            layout.focus_bbox = merge_layout_focus_bbox(layout, analysis.focus_bbox);
            merge_cluster_ids(layout, analysis);
            if layout.state == LayoutState::Tentative
                && layout.support_count >= self.options.config.layout_established_after_matches
            {
                layout.state = LayoutState::Established;
                events.push(format!("layout_established:{}", layout.id));
            }
            events.push(format!("layout_matched:{}:{best_score:.3}", layout.id));
            return Some(layout.id.clone());
        }

        let id = format!("layout_{}", self.profile.layout_candidates.len() + 1);
        let layout = LayoutCandidate {
            id: id.clone(),
            kind: analysis.layout_kind.clone(),
            state: LayoutState::Tentative,
            cluster_ids: active_ids.iter().cloned().collect(),
            dialogue_cluster_ids: analysis
                .active_clusters
                .iter()
                .filter(|cluster| {
                    matches!(
                        cluster.class,
                        RegionClass::Dialogue
                            | RegionClass::Choice
                            | RegionClass::BacklogText
                            | RegionClass::OverlayFocus
                    )
                })
                .map(|cluster| cluster.id.clone())
                .collect(),
            name_cluster_ids: analysis
                .active_clusters
                .iter()
                .filter(|cluster| cluster.class == RegionClass::Name)
                .map(|cluster| cluster.id.clone())
                .collect(),
            ui_cluster_ids: analysis
                .active_clusters
                .iter()
                .filter(|cluster| cluster.class == RegionClass::StaticUi)
                .map(|cluster| cluster.id.clone())
                .collect(),
            support_count: 1,
            recent_support: 1,
            first_seen_frame: frame_index,
            last_seen_frame: frame_index,
            confidence: 0.25,
            focus_bbox: analysis.focus_bbox,
            occluded_base_layout_id: analysis.occluded_base_layout_id.clone(),
        };
        self.profile.layout_candidates.push(layout);
        events.push(format!("layout_created:{id}:{:?}", analysis.layout_kind));
        Some(id)
    }

    fn expire_tentative_layouts(&mut self, frame_index: u64, events: &mut Vec<String>) {
        for layout in &mut self.profile.layout_candidates {
            if layout.state == LayoutState::Tentative
                && layout.last_seen_frame + self.options.config.tentative_layout_expire_after_frames < frame_index
            {
                layout.state = LayoutState::Retired;
                layout.confidence = 0.0;
                events.push(format!("layout_retired:{}", layout.id));
            }
        }
    }

    fn predict_ignore_regions(&self, width: u32, height: u32, events: &mut Vec<String>) -> Vec<PredictedRegion> {
        let mut regions = Vec::new();
        for cluster in &self.profile.region_memories {
            if !is_cluster_recent_for_output(cluster, self.profile.frame_count) {
                continue;
            }
            let reason = match cluster.classification {
                RegionClass::Name if cluster.count >= self.options.config.ignore_static_after_observations / 2 => {
                    Some("stable_speaker_candidate")
                }
                RegionClass::StaticUi
                    if should_ignore_static_ui(cluster, self.options.config.ignore_static_after_observations) =>
                {
                    Some("stable_low_volatility_ui")
                }
                RegionClass::DecorativeOrNoise if cluster.count >= self.options.config.ignore_static_after_observations => {
                    Some("decorative_or_noise")
                }
                _ => None,
            };

            if let Some(reason) = reason {
                events.push(format!("ignore_promoted:{}:{reason}", cluster.id));
                regions.push(PredictedRegion {
                    id: cluster.id.clone(),
                    bbox: denormalize_bbox(cluster.mean_bbox, width, height),
                    confidence: cluster.confidence,
                    purpose: None,
                    reason: Some(reason.to_string()),
                });
            }
        }
        regions.sort_by(|a, b| {
            let a_cluster = self.profile.region_memories.iter().find(|cluster| cluster.id == a.id);
            let b_cluster = self.profile.region_memories.iter().find(|cluster| cluster.id == b.id);
            let a_seen = a_cluster.map(|cluster| cluster.last_seen_frame).unwrap_or(0);
            let b_seen = b_cluster.map(|cluster| cluster.last_seen_frame).unwrap_or(0);
            let a_recent = a_cluster.map(|cluster| cluster.recent_count).unwrap_or(0);
            let b_recent = b_cluster.map(|cluster| cluster.recent_count).unwrap_or(0);
            b_seen
                .cmp(&a_seen)
                .then_with(|| b_recent.cmp(&a_recent))
                .then_with(|| b.confidence.total_cmp(&a.confidence))
        });
        regions.truncate(self.options.config.max_ignore_regions as usize);
        regions
    }

    fn classified_regions(&self, ocr_regions: &[PredictedRegion], width: u32, height: u32) -> ClassifiedRegions {
        let mut classified = ClassifiedRegions {
            dialogue: ocr_regions.to_vec(),
            ..ClassifiedRegions::default()
        };

        for cluster in &self.profile.region_memories {
            if !is_cluster_recent_for_output(cluster, self.profile.frame_count) {
                continue;
            }
            let region = PredictedRegion {
                id: cluster.id.clone(),
                bbox: denormalize_bbox(cluster.mean_bbox, width, height),
                confidence: cluster.confidence,
                purpose: Some(format!("{:?}", cluster.classification).to_lowercase()),
                reason: Some(class_reason(cluster).to_string()),
            };
            match cluster.classification {
                RegionClass::Dialogue
                | RegionClass::Choice
                | RegionClass::BacklogText
                | RegionClass::OverlayFocus => {
                    if !classified.dialogue.iter().any(|existing| existing.id == region.id) {
                        classified.dialogue.push(region.clone());
                    }
                }
                RegionClass::Name => classified.names.push(region.clone()),
                RegionClass::StaticUi
                    if should_ignore_static_ui(cluster, self.options.config.ignore_static_after_observations) =>
                {
                    classified.ui.push(region.clone())
                }
                RegionClass::StaticUi => {}
                RegionClass::DecorativeOrNoise => classified.non_dialogue.push(region.clone()),
                RegionClass::Unknown => {}
            }
            if matches!(
                cluster.classification,
                RegionClass::Name | RegionClass::DecorativeOrNoise
            ) {
                classified.non_dialogue.push(region);
            } else if cluster.classification == RegionClass::StaticUi
                && should_ignore_static_ui(cluster, self.options.config.ignore_static_after_observations)
            {
                classified.non_dialogue.push(region);
            }
        }

        classified
    }
}

fn new_cluster(
    id: String,
    region: &RegionObservation,
    source: &str,
    bbox: BBox,
    frame_index: u64,
) -> RegionCluster {
    let mut role_hint_counts = RoleHintCounts::default();
    apply_role_hint(&mut role_hint_counts, region.kind_hint.as_deref());
    let text_len = region.text.chars().count() as u64;
    RegionCluster {
        id,
        source: source.to_string(),
        mean_bbox: bbox,
        variance_bbox: [0.0; 4],
        count: 1,
        recent_count: 1,
        last_seen_frame: frame_index,
        last_text: region.text.clone(),
        text_change_count: 0,
        unique_text_count: if region.text.is_empty() { 0 } else { 1 },
        repeated_text_count: 0,
        empty_text_count: if region.text.is_empty() { 1 } else { 0 },
        text_len_sum: text_len,
        text_len_square_sum: text_len * text_len,
        avg_confidence: region.confidence,
        vertical_count: u64::from(region.is_vertical),
        text_source_count: u64::from(source == "text"),
        ui_source_count: u64::from(source == "ui"),
        role_hint_counts,
        classification: RegionClass::Unknown,
        confidence: region.confidence.min(0.4),
    }
}

fn update_cluster_stats(
    cluster: &mut RegionCluster,
    region: &RegionObservation,
    source: &str,
    bbox: BBox,
    frame_index: u64,
) {
    let old_count = cluster.count as f64;
    let new_count = old_count + 1.0;
    for coordinate in 0..4 {
        let old_mean = cluster.mean_bbox[coordinate];
        let new_mean = ((old_mean * old_count) + bbox[coordinate]) / new_count;
        let delta = bbox[coordinate] - old_mean;
        cluster.variance_bbox[coordinate] =
            ((cluster.variance_bbox[coordinate] * old_count) + delta * delta) / new_count;
        cluster.mean_bbox[coordinate] = new_mean;
    }

    if region.text.is_empty() {
        cluster.empty_text_count += 1;
    } else if cluster.last_text == region.text {
        cluster.repeated_text_count += 1;
    } else {
        if !cluster.last_text.is_empty() {
            cluster.text_change_count += 1;
        }
        cluster.unique_text_count += 1;
        cluster.last_text = region.text.clone();
    }

    let text_len = region.text.chars().count() as u64;
    cluster.text_len_sum += text_len;
    cluster.text_len_square_sum += text_len * text_len;
    cluster.count += 1;
    cluster.recent_count += 1;
    cluster.last_seen_frame = frame_index;
    cluster.avg_confidence = ((cluster.avg_confidence * old_count) + region.confidence) / new_count;
    cluster.vertical_count += u64::from(region.is_vertical);
    cluster.text_source_count += u64::from(source == "text");
    cluster.ui_source_count += u64::from(source == "ui");
    apply_role_hint(&mut cluster.role_hint_counts, region.kind_hint.as_deref());
}

fn classify_cluster(cluster: &mut RegionCluster) {
    let area = bbox_area(cluster.mean_bbox);
    let width = (cluster.mean_bbox[2] - cluster.mean_bbox[0]).max(0.0);
    let height = (cluster.mean_bbox[3] - cluster.mean_bbox[1]).max(0.0);
    let y_center = (cluster.mean_bbox[1] + cluster.mean_bbox[3]) / 2.0;
    let volatility = text_volatility(cluster);
    let avg_len = avg_text_len(cluster);
    let stability = geometry_stability(cluster);
    let source_ui_ratio = cluster.ui_source_count as f64 / cluster.count.max(1) as f64;
    let vertical_ratio = cluster.vertical_count as f64 / cluster.count.max(1) as f64;
    let repeated_stable_text = stable_repeated_text_score(cluster, stability) >= 0.72;
    let name_like =
        area < 0.035 && width <= 0.18 && avg_len <= 12.0 && (0.55..=0.74).contains(&y_center) && cluster.count >= 2;
    let dialogue_band_text_like =
        width >= 0.12 && height >= 0.025 && (0.68..=0.92).contains(&y_center) && avg_len >= 5.0;
    let tiny_stable_text = stability >= 0.82 && area < 0.012 && width <= 0.10 && avg_len <= 8.0 && cluster.count >= 2;

    let class = if cluster.avg_confidence < 0.25 || (cluster.count <= 2 && stability < 0.35) {
        RegionClass::DecorativeOrNoise
    } else if name_like {
        RegionClass::Name
    } else if dialogue_band_text_like {
        RegionClass::Dialogue
    } else if tiny_stable_text {
        RegionClass::StaticUi
    } else if source_ui_ratio > 0.5 || repeated_stable_text {
        RegionClass::StaticUi
    } else if width >= 0.18 && height >= 0.025 && (0.68..=0.90).contains(&y_center) && avg_len >= 8.0 {
        RegionClass::Dialogue
    } else if y_center > 0.93 && cluster.count >= 4 {
        RegionClass::StaticUi
    } else if cluster.role_hint_counts.choice > 0 || (cluster.count >= 3 && area < 0.12 && avg_len >= 1.0 && volatility > 0.25) {
        RegionClass::Choice
    } else if volatility < 0.12 && cluster.count >= 4 && avg_len <= 16.0 {
        RegionClass::StaticUi
    } else if area > 0.12 && cluster.mean_bbox[1] < 0.72 && avg_len >= 8.0 {
        RegionClass::OverlayFocus
    } else if area > 0.25 && avg_len >= 8.0 {
        RegionClass::BacklogText
    } else if area > 0.035 && (volatility > 0.18 || avg_len >= 12.0 || vertical_ratio > 0.6) {
        RegionClass::Dialogue
    } else {
        RegionClass::Unknown
    };

    cluster.classification = class;
    cluster.confidence = cluster_confidence(cluster, stability, volatility);
}

fn analyze_frame(
    usable_text: &[&RegionObservation],
    active_clusters: &[ActiveCluster],
    width: u32,
    height: u32,
    layouts: &[LayoutCandidate],
) -> FrameAnalysis {
    let mut events = Vec::new();
    let text_boxes = usable_text
        .iter()
        .map(|region| normalize_bbox(region.bbox, width, height))
        .collect::<Vec<_>>();
    let current_union = union_bbox(&text_boxes);
    let semantic_boxes = active_clusters
        .iter()
        .filter(|cluster| {
            matches!(
                cluster.class,
                RegionClass::Dialogue
                    | RegionClass::Choice
                    | RegionClass::BacklogText
                    | RegionClass::OverlayFocus
            )
        })
        .map(|cluster| cluster.bbox)
        .collect::<Vec<_>>();
    let geometry_dialogue_boxes = usable_text
        .iter()
        .map(|region| normalize_bbox(region.bbox, width, height))
        .filter(|bbox| is_geometry_dialogue_candidate(*bbox))
        .collect::<Vec<_>>();
    let semantic_union = union_bbox(&[semantic_boxes, geometry_dialogue_boxes].concat());
    let layout_kind = detect_layout_kind(
        usable_text,
        current_union,
        semantic_union,
        active_clusters,
        layouts,
        width,
        height,
    );
    let mut focus_bbox = match layout_kind {
        LayoutKind::Backlog => current_union,
        LayoutKind::OverlayPanel => semantic_union,
        LayoutKind::Unknown => None,
        _ => active_clusters
            .iter()
            .filter(|cluster| {
                matches!(
                    cluster.class,
                    RegionClass::Dialogue
                        | RegionClass::Choice
                        | RegionClass::BacklogText
                        | RegionClass::OverlayFocus
                )
            })
            .map(|cluster| cluster.bbox)
            .collect::<Vec<_>>()
            .as_slice()
            .pipe(union_bbox)
            .or(semantic_union)
            .or(current_union),
    };

    if let Some(bbox) = focus_bbox {
        focus_bbox = Some(expand_bbox(bbox, 0.025));
    }

    if layout_kind == LayoutKind::Backlog {
        events.push("backlog_detected".to_string());
    }

    let occluded_base_layout_id = if layout_kind == LayoutKind::OverlayPanel {
        events.push("overlay_detected".to_string());
        semantic_union.and_then(|overlay| {
            layouts
                .iter()
                .filter(|layout| layout.state == LayoutState::Established)
                .find(|layout| layout.focus_bbox.is_some_and(|base| iou(base, overlay) > 0.05))
                .map(|layout| layout.id.clone())
        })
    } else {
        None
    };

    FrameAnalysis {
        layout_kind,
        active_clusters: active_clusters.to_vec(),
        focus_bbox,
        occluded_base_layout_id,
        events,
    }
}

fn detect_layout_kind(
    usable_text: &[&RegionObservation],
    current_union: Option<BBox>,
    semantic_union: Option<BBox>,
    active_clusters: &[ActiveCluster],
    layouts: &[LayoutCandidate],
    width: u32,
    height: u32,
) -> LayoutKind {
    let Some(union) = current_union else {
        return LayoutKind::Unknown;
    };
    let union_height = (union[3] - union[1]).max(0.0);
    let union_width = (union[2] - union[0]).max(0.0);
    let union_area = bbox_area(union);
    let text_count = usable_text.len();
    let has_established_base = layouts
        .iter()
        .any(|layout| layout.state == LayoutState::Established && layout.kind == LayoutKind::NormalDialogue);

    let row_count = approximate_text_rows(usable_text, union, width, height);

    if (text_count >= 5 && union_height > 0.42 && union_area > 0.18)
        || (text_count >= 8 && row_count >= 4 && union_height > 0.30 && union_area > 0.12)
        || (text_count >= 12 && row_count >= 3 && union_area > 0.10)
    {
        return LayoutKind::Backlog;
    }
    let Some(semantic) = semantic_union else {
        if text_count <= 2 && union[1] < 0.65 && union_area < 0.20 && union_width >= 0.18 {
            return LayoutKind::NarrationOrSystem;
        }
        return LayoutKind::Unknown;
    };
    let semantic_area = bbox_area(semantic);
    if has_established_base && semantic_area > 0.10 && semantic[1] < 0.70 && semantic[3] < 0.92 {
        return LayoutKind::OverlayPanel;
    }
    let dialogue_like = active_clusters
        .iter()
        .filter(|cluster| matches!(cluster.class, RegionClass::Dialogue | RegionClass::Choice))
        .count();
    if text_count >= 3 && dialogue_like >= 3 && union_area < 0.30 {
        return LayoutKind::ChoiceMenu;
    }
    if text_count <= 2 && union[1] < 0.65 && union_area < 0.20 {
        return LayoutKind::NarrationOrSystem;
    }
    LayoutKind::NormalDialogue
}

fn is_geometry_dialogue_candidate(bbox: BBox) -> bool {
    let width = (bbox[2] - bbox[0]).max(0.0);
    let height = (bbox[3] - bbox[1]).max(0.0);
    let y_center = (bbox[1] + bbox[3]) / 2.0;
    width >= 0.12 && height >= 0.018 && (0.58..=0.92).contains(&y_center)
}

fn approximate_text_rows(
    usable_text: &[&RegionObservation],
    union: BBox,
    width: u32,
    height: u32,
) -> usize {
    let mut rows: Vec<f64> = Vec::new();
    let threshold = ((union[3] - union[1]).max(0.05) / 18.0).clamp(0.018, 0.055);
    for region in usable_text {
        let bbox = normalize_bbox(region.bbox, width, height);
        let y = (bbox[1] + bbox[3]) / 2.0;
        if rows.iter().all(|existing| (existing - y).abs() > threshold) {
            rows.push(y);
        }
    }
    rows.len()
}

fn merge_cluster_ids(layout: &mut LayoutCandidate, analysis: &FrameAnalysis) {
    merge_ids(
        &mut layout.cluster_ids,
        analysis
            .active_clusters
            .iter()
            .filter(|cluster| is_layout_evidence_class(&cluster.class))
            .map(|cluster| cluster.id.clone()),
    );
    merge_ids(
        &mut layout.dialogue_cluster_ids,
        analysis
            .active_clusters
            .iter()
            .filter(|cluster| {
                matches!(
                    cluster.class,
                    RegionClass::Dialogue
                        | RegionClass::Choice
                        | RegionClass::BacklogText
                        | RegionClass::OverlayFocus
                )
            })
            .map(|cluster| cluster.id.clone()),
    );
    merge_ids(
        &mut layout.name_cluster_ids,
        analysis
            .active_clusters
            .iter()
            .filter(|cluster| cluster.class == RegionClass::Name)
            .map(|cluster| cluster.id.clone()),
    );
    merge_ids(
        &mut layout.ui_cluster_ids,
        analysis
            .active_clusters
            .iter()
            .filter(|cluster| cluster.class == RegionClass::StaticUi)
            .map(|cluster| cluster.id.clone()),
    );
}

fn layout_evidence_ids(analysis: &FrameAnalysis) -> BTreeSet<String> {
    analysis
        .active_clusters
        .iter()
        .filter(|cluster| is_layout_evidence_class(&cluster.class))
        .map(|cluster| cluster.id.clone())
        .collect()
}

fn is_layout_evidence_class(class: &RegionClass) -> bool {
    matches!(
        class,
        RegionClass::Dialogue
            | RegionClass::Choice
            | RegionClass::BacklogText
            | RegionClass::OverlayFocus
            | RegionClass::Name
    )
}

fn is_cluster_recent_for_output(cluster: &RegionCluster, frame_count: u64) -> bool {
    cluster.last_seen_frame + OUTPUT_RECENT_FRAME_WINDOW >= frame_count
}

fn merge_ids(target: &mut Vec<String>, incoming: impl Iterator<Item = String>) {
    let mut ids = target.iter().cloned().collect::<BTreeSet<_>>();
    for id in incoming {
        ids.insert(id);
    }
    *target = ids.into_iter().collect();
}

fn layout_match_score(
    layout: &LayoutCandidate,
    analysis: &FrameAnalysis,
    active_ids: &BTreeSet<String>,
) -> f64 {
    let layout_ids = layout.cluster_ids.iter().cloned().collect::<BTreeSet<_>>();
    let intersection = active_ids.intersection(&layout_ids).count() as f64;
    let union = active_ids.union(&layout_ids).count().max(1) as f64;
    let cluster_score = intersection / union;

    let focus_score = match (layout.focus_bbox, analysis.focus_bbox) {
        (Some(layout_bbox), Some(current_bbox)) => {
            let overlap = iou(layout_bbox, current_bbox);
            let containment = bbox_containment(layout_bbox, current_bbox);
            overlap.max(containment * 0.9)
        }
        _ => 0.0,
    };

    if layout.kind != analysis.layout_kind && layout.kind != LayoutKind::Unknown {
        return 0.0;
    }

    match analysis.layout_kind {
        LayoutKind::Backlog => cluster_score.max(focus_score),
        LayoutKind::OverlayPanel => cluster_score.max(focus_score * 0.9),
        LayoutKind::NormalDialogue | LayoutKind::ChoiceMenu | LayoutKind::NarrationOrSystem => {
            cluster_score.max(focus_score * 0.95)
        }
        LayoutKind::BattleDialogue | LayoutKind::Unknown => cluster_score.max(focus_score * 0.75),
    }
}

fn merge_layout_focus_bbox(layout: &LayoutCandidate, incoming: Option<BBox>) -> Option<BBox> {
    match layout.kind {
        LayoutKind::NormalDialogue | LayoutKind::ChoiceMenu | LayoutKind::NarrationOrSystem => {
            merge_focus_envelope(layout.focus_bbox, incoming)
        }
        LayoutKind::Backlog | LayoutKind::OverlayPanel | LayoutKind::BattleDialogue | LayoutKind::Unknown => {
            merge_optional_bbox(layout.focus_bbox, incoming, layout.support_count)
        }
    }
}

fn bbox_containment(a: BBox, b: BBox) -> f64 {
    let intersection = [
        a[0].max(b[0]),
        a[1].max(b[1]),
        a[2].min(b[2]),
        a[3].min(b[3]),
    ];
    let intersection_area = bbox_area(intersection);
    let smaller_area = bbox_area(a).min(bbox_area(b));
    if smaller_area <= 0.0 {
        0.0
    } else {
        (intersection_area / smaller_area).clamp(0.0, 1.0)
    }
}

fn merge_focus_envelope(existing: Option<BBox>, incoming: Option<BBox>) -> Option<BBox> {
    match (existing, incoming) {
        (Some(old), Some(new)) => {
            let expanded = union_bbox(&[old, new]).unwrap_or(old);
            let old_area = bbox_area(old);
            let expanded_area = bbox_area(expanded);
            let max_area = match old_area {
                area if area < 0.02 => 0.22,
                area => (area * 2.8).max(0.22).min(0.45),
            };
            if expanded_area <= max_area && center_distance(old, new) <= 0.35 {
                Some(expanded)
            } else {
                Some(old)
            }
        }
        (None, Some(new)) => Some(new),
        (Some(old), None) => Some(old),
        (None, None) => None,
    }
}

fn merge_optional_bbox(existing: Option<BBox>, incoming: Option<BBox>, count: u64) -> Option<BBox> {
    match (existing, incoming) {
        (Some(old), Some(new)) => {
            let old_weight = count.saturating_sub(1) as f64;
            let new_weight = count.max(1) as f64;
            Some([
                ((old[0] * old_weight) + new[0]) / new_weight,
                ((old[1] * old_weight) + new[1]) / new_weight,
                ((old[2] * old_weight) + new[2]) / new_weight,
                ((old[3] * old_weight) + new[3]) / new_weight,
            ])
        }
        (None, Some(new)) => Some(new),
        (Some(old), None) => Some(old),
        (None, None) => None,
    }
}

fn predict_current_frame_bbox(
    regions: &[&RegionObservation],
    width: u32,
    height: u32,
    padding_ratio: f64,
) -> Option<BBox> {
    let boxes = regions
        .iter()
        .map(|region| normalize_bbox(region.bbox, width, height))
        .collect::<Vec<_>>();
    union_bbox(&boxes).map(|bbox| expand_bbox(bbox, padding_ratio))
}

fn prediction_bbox(
    layout: Option<&LayoutCandidate>,
    analysis_bbox: Option<BBox>,
    fallback_bbox: Option<BBox>,
    padding_ratio: f64,
) -> Option<BBox> {
    let current = analysis_bbox.or(fallback_bbox);
    let learned = layout.and_then(|layout| layout.focus_bbox);
    let combined = match (layout, learned, current) {
        (Some(layout), Some(learned), Some(current))
            if layout.state == LayoutState::Established
                && matches!(
                    layout.kind,
                    LayoutKind::NormalDialogue | LayoutKind::ChoiceMenu | LayoutKind::NarrationOrSystem
                ) =>
        {
            union_bbox(&[learned, current])
        }
        (Some(layout), Some(learned), Some(current)) if layout.kind == LayoutKind::Backlog => {
            union_bbox(&[learned, current])
        }
        (Some(_), Some(learned), None) => Some(learned),
        (_, _, Some(current)) => Some(current),
        (_, Some(learned), None) => Some(learned),
        _ => None,
    };
    combined.map(|bbox| expand_bbox(bbox, padding_ratio.max(0.035)))
}

fn prediction_mode_and_confidence(
    bbox: Option<BBox>,
    regions: &[&RegionObservation],
    layout: Option<&LayoutCandidate>,
    frame_count: u64,
    config: &model::ProfilerConfig,
) -> (String, f64) {
    if bbox.is_none() {
        return ("fallback".to_string(), 0.0);
    }

    let avg_confidence = if regions.is_empty() {
        0.0
    } else {
        regions.iter().map(|region| region.confidence).sum::<f64>() / regions.len() as f64
    };

    if let Some(layout) = layout {
        let mode = match layout.state {
            LayoutState::Established => "established",
            LayoutState::Tentative => "tentative",
            LayoutState::Retired => "fallback",
        };
        let confidence = (layout.confidence * 0.8 + avg_confidence * 0.2).min(0.98);
        return (mode.to_string(), confidence);
    }

    let (mode, base) = if frame_count >= config.established_after_observations {
        ("tentative", 0.55)
    } else if frame_count >= config.exploratory_after_observations {
        ("tentative", 0.45)
    } else {
        ("exploratory", 0.25)
    };
    (mode.to_string(), (base + avg_confidence * 0.2).min(0.85))
}

fn apply_role_hint(counts: &mut RoleHintCounts, hint: Option<&str>) {
    match hint.unwrap_or("unknown") {
        "dialogue" => counts.dialogue += 1,
        "speaker" | "name" => counts.speaker += 1,
        "ui" => counts.ui += 1,
        "choice" => counts.choice += 1,
        "system" => counts.system += 1,
        "decorative" => counts.decorative += 1,
        _ => counts.unknown += 1,
    }
}

fn avg_text_len(cluster: &RegionCluster) -> f64 {
    cluster.text_len_sum as f64 / cluster.count.max(1) as f64
}

fn text_volatility(cluster: &RegionCluster) -> f64 {
    cluster.text_change_count as f64 / cluster.count.saturating_sub(1).max(1) as f64
}

fn should_ignore_static_ui(cluster: &RegionCluster, configured_threshold: u64) -> bool {
    let stability = geometry_stability(cluster);
    if cluster.count >= configured_threshold {
        return true;
    }

    let source_ui_ratio = cluster.ui_source_count as f64 / cluster.count.max(1) as f64;
    let fast_threshold = if source_ui_ratio > 0.5 { 2 } else { 3 };
    cluster.count >= fast_threshold && stable_repeated_text_score(cluster, stability) >= 0.68
}

fn stable_repeated_text_score(cluster: &RegionCluster, stability: f64) -> f64 {
    if cluster.count < 2 || cluster.last_text.trim().is_empty() {
        return 0.0;
    }

    let avg_len = avg_text_len(cluster);
    if avg_len > 32.0 {
        return 0.0;
    }

    let repeated_ratio = cluster.repeated_text_count as f64 / cluster.count.saturating_sub(1).max(1) as f64;
    let uniqueness_penalty = ((cluster.unique_text_count.saturating_sub(1)) as f64 * 0.20).min(0.6);
    let volatility_penalty = (text_volatility(cluster) * 0.9).min(0.8);

    (stability * 0.45 + repeated_ratio * 0.55 - uniqueness_penalty - volatility_penalty).clamp(0.0, 1.0)
}

fn geometry_stability(cluster: &RegionCluster) -> f64 {
    let variance = cluster.variance_bbox.iter().sum::<f64>() / 4.0;
    (1.0 - variance.sqrt() * 25.0).clamp(0.0, 1.0)
}

fn cluster_confidence(cluster: &RegionCluster, stability: f64, volatility: f64) -> f64 {
    let support = (cluster.count as f64 / 20.0).min(1.0);
    let quality = cluster.avg_confidence.clamp(0.0, 1.0);
    let behavior = match cluster.classification {
        RegionClass::Dialogue | RegionClass::Choice | RegionClass::BacklogText | RegionClass::OverlayFocus => {
            volatility.max(0.2)
        }
        RegionClass::Name | RegionClass::StaticUi => (1.0 - volatility).max(0.2),
        RegionClass::DecorativeOrNoise => 0.2,
        RegionClass::Unknown => 0.3,
    };
    (support * 0.35 + stability * 0.30 + quality * 0.20 + behavior * 0.15).clamp(0.0, 0.98)
}

fn class_reason(cluster: &RegionCluster) -> &'static str {
    match cluster.classification {
        RegionClass::Dialogue => "volatile_dialogue_like_region",
        RegionClass::Name => "small_stable_name_like_region",
        RegionClass::StaticUi => "stable_low_volatility_ui",
        RegionClass::Choice => "choice_like_text_group",
        RegionClass::BacklogText => "large_backlog_text_region",
        RegionClass::OverlayFocus => "large_overlay_focus_region",
        RegionClass::DecorativeOrNoise => "low_confidence_or_unstable_noise",
        RegionClass::Unknown => "insufficient_evidence",
    }
}

fn layout_kind_reason(kind: &LayoutKind) -> &'static str {
    match kind {
        LayoutKind::NormalDialogue => "normal_dialogue",
        LayoutKind::ChoiceMenu => "choice_menu",
        LayoutKind::NarrationOrSystem => "narration_or_system",
        LayoutKind::Backlog => "backlog",
        LayoutKind::BattleDialogue => "battle_dialogue",
        LayoutKind::OverlayPanel => "overlay_panel",
        LayoutKind::Unknown => "unknown_layout",
    }
}

fn load_or_create_profile(path: &Path, profile_id: &str) -> Result<GameProfile> {
    if path.exists() {
        let data = fs::read(path)?;
        let mut profile: GameProfile = serde_json::from_slice(&data)?;
        if profile.profile_id.is_empty() {
            profile.profile_id = profile_id.to_string();
        }
        return Ok(profile);
    }

    Ok(GameProfile {
        schema_version: 1,
        implementation: "rust_v1".to_string(),
        profile_id: profile_id.to_string(),
        frame_count: 0,
        empty_frame_streak: 0,
        last_frame_id: None,
        last_timestamp_ms: None,
        last_frame_width: None,
        last_frame_height: None,
        region_memories: Vec::new(),
        layout_candidates: Vec::new(),
        last_prediction: None,
    })
}

fn tmp_path_for(path: &Path) -> PathBuf {
    let mut tmp = path.as_os_str().to_os_string();
    tmp.push(".tmp");
    PathBuf::from(tmp)
}

trait Pipe: Sized {
    fn pipe<T>(self, f: impl FnOnce(Self) -> T) -> T {
        f(self)
    }
}

impl<T> Pipe for T {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{FrameInfo, ProfilerConfig};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_PROFILE_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn test_profile_path(label: &str) -> std::path::PathBuf {
        let counter = TEST_PROFILE_COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "dialogue_layout_profiler_{label}_{}_{}.json",
            std::process::id(),
            counter
        ))
    }

    fn profiler() -> DialogueLayoutProfiler {
        let temp_path = test_profile_path("test");
        DialogueLayoutProfiler::open(ProfileOpenOptions {
            profile_id: "test".to_string(),
            profile_path: temp_path.to_string_lossy().to_string(),
            flush_policy: FlushPolicy::Manual,
            config: ProfilerConfig {
                layout_established_after_matches: 3,
                ignore_static_after_observations: 3,
                ..ProfilerConfig::default()
            },
        })
        .expect("profiler opens")
    }

    fn frame(regions: Vec<RegionObservation>) -> FrameObservation {
        FrameObservation {
            frame_id: Some("f".to_string()),
            timestamp_ms: Some(100.0),
            frame: FrameInfo {
                width: 1920,
                height: 1080,
            },
            regions,
            ui_regions: Vec::new(),
        }
    }

    fn frame_720(regions: Vec<RegionObservation>) -> FrameObservation {
        FrameObservation {
            frame_id: Some("f".to_string()),
            timestamp_ms: Some(100.0),
            frame: FrameInfo {
                width: 1280,
                height: 720,
            },
            regions,
            ui_regions: Vec::new(),
        }
    }

    fn frame_with_ui(regions: Vec<RegionObservation>, ui_regions: Vec<RegionObservation>) -> FrameObservation {
        FrameObservation {
            frame_id: Some("f".to_string()),
            timestamp_ms: Some(100.0),
            frame: FrameInfo {
                width: 1920,
                height: 1080,
            },
            regions,
            ui_regions,
        }
    }

    fn region(id: &str, bbox: [f64; 4], text: &str) -> RegionObservation {
        RegionObservation {
            id: Some(id.to_string()),
            bbox,
            text: text.to_string(),
            confidence: 0.9,
            is_vertical: false,
            chars: Vec::new(),
            kind_hint: None,
        }
    }

    fn hinted_region(id: &str, bbox: [f64; 4], text: &str, hint: &str) -> RegionObservation {
        RegionObservation {
            kind_hint: Some(hint.to_string()),
            ..region(id, bbox, text)
        }
    }

    #[test]
    fn predicts_expanded_region_from_text_union() {
        let mut profiler = profiler();
        let prediction = profiler
            .observe_frame(frame(vec![region("dialogue", [400.0, 760.0, 1500.0, 930.0], "Hello")]))
            .expect("frame observed");

        assert_eq!(prediction.mode, "tentative");
        assert_eq!(prediction.ocr_regions.len(), 1);
        assert!(prediction.ocr_regions[0].bbox[0] < 400);
    }

    #[test]
    fn parses_meiki_char_field() {
        let json = r#"{"char":"あ","bbox":[1,2,3,4],"confidence":0.98}"#;
        let parsed: crate::model::CharObservation = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.value, "あ");
    }

    #[test]
    fn stable_dialogue_converges_to_established_layout() {
        let mut profiler = profiler();
        let mut last = None;
        for index in 0..4 {
            last = Some(
                profiler
                    .observe_frame(frame(vec![region(
                        "dialogue",
                        [400.0, 760.0, 1500.0, 930.0],
                        &format!("Line {index}"),
                    )]))
                    .unwrap(),
            );
        }
        let prediction = last.unwrap();
        assert_eq!(prediction.mode, "established");
        assert!(prediction.active_layout_id.is_some());
    }

    #[test]
    fn established_layout_checkpoints_profile_even_with_manual_flush_policy() {
        let temp_path = test_profile_path("checkpoint_test");
        let _ = std::fs::remove_file(&temp_path);
        let mut profiler = DialogueLayoutProfiler::open(ProfileOpenOptions {
            profile_id: "test".to_string(),
            profile_path: temp_path.to_string_lossy().to_string(),
            flush_policy: FlushPolicy::Manual,
            config: ProfilerConfig {
                layout_established_after_matches: 3,
                ..ProfilerConfig::default()
            },
        })
        .expect("profiler opens");

        for index in 0..4 {
            profiler
                .observe_frame(frame(vec![region(
                    "dialogue",
                    [400.0, 760.0, 1500.0, 930.0],
                    &format!("Line {index}"),
                )]))
                .unwrap();
        }

        assert!(temp_path.exists());
        let _ = std::fs::remove_file(temp_path);
    }

    #[test]
    fn speaker_name_becomes_classified_name() {
        let mut profiler = profiler();
        let mut prediction = None;
        for index in 0..4 {
            prediction = Some(
                profiler
                    .observe_frame(frame(vec![
                        region("speaker", [430.0, 695.0, 690.0, 745.0], "Luca"),
                        region("dialogue", [420.0, 760.0, 1510.0, 940.0], &format!("Line {index}")),
                    ]))
                    .unwrap(),
            );
        }
        let prediction = prediction.unwrap();
        assert!(!prediction.speaker_regions.is_empty());
        assert!(!prediction.classified_regions.names.is_empty());
    }

    #[test]
    fn backlog_screen_is_broad_layout() {
        let mut profiler = profiler();
        let regions = (0..6)
            .map(|index| {
                region(
                    &format!("r{index}"),
                    [250.0, 120.0 + index as f64 * 120.0, 1660.0, 190.0 + index as f64 * 120.0],
                    &format!("Past line {index}"),
                )
            })
            .collect::<Vec<_>>();
        let prediction = profiler.observe_frame(frame(regions)).unwrap();
        assert_eq!(prediction.ocr_regions[0].purpose.as_deref(), Some("backlog"));
    }

    #[test]
    fn overlay_focus_does_not_remove_base_layout() {
        let mut profiler = profiler();
        for index in 0..4 {
            profiler
                .observe_frame(frame(vec![region(
                    "dialogue",
                    [400.0, 760.0, 1500.0, 930.0],
                    &format!("Line {index}"),
                )]))
                .unwrap();
        }
        let base_count = profiler.profile.layout_candidates.len();
        let prediction = profiler
            .observe_frame(frame(vec![region(
                "dictionary",
                [520.0, 240.0, 1500.0, 760.0],
                "Dictionary explanation text",
            )]))
            .unwrap();
        assert_eq!(prediction.ocr_regions[0].purpose.as_deref(), Some("overlay_focus"));
        assert!(profiler.profile.layout_candidates.len() >= base_count);
    }

    #[test]
    fn static_ui_becomes_ignored_ui_region() {
        let mut profiler = profiler();
        let mut prediction = None;
        for index in 0..4 {
            prediction = Some(
                profiler
                    .observe_frame(frame_with_ui(
                        vec![region("dialogue", [420.0, 760.0, 1510.0, 940.0], &format!("Line {index}"))],
                        vec![hinted_region("skip", [1710.0, 970.0, 1845.0, 1030.0], "Skip", "ui")],
                    ))
                    .unwrap(),
            );
        }
        let prediction = prediction.unwrap();
        assert!(prediction
            .ignore_regions
            .iter()
            .any(|region| region.reason.as_deref() == Some("stable_low_volatility_ui")));
        assert!(!prediction.classified_regions.ui.is_empty());
    }

    #[test]
    fn choice_menu_forms_separate_layout() {
        let mut profiler = profiler();
        let mut prediction = None;
        for index in 0..4 {
            prediction = Some(
                profiler
                    .observe_frame(frame(vec![
                        hinted_region("choice1", [620.0, 610.0, 1300.0, 665.0], &format!("Go left {index}"), "choice"),
                        hinted_region("choice2", [620.0, 690.0, 1300.0, 745.0], &format!("Go right {index}"), "choice"),
                        hinted_region("choice3", [620.0, 770.0, 1300.0, 825.0], &format!("Wait {index}"), "choice"),
                    ]))
                    .unwrap(),
            );
        }
        let prediction = prediction.unwrap();
        assert_eq!(prediction.ocr_regions[0].purpose.as_deref(), Some("choice"));
        assert!(profiler
            .profile
            .layout_candidates
            .iter()
            .any(|layout| layout.kind == LayoutKind::ChoiceMenu));
    }

    #[test]
    fn chaotic_regions_do_not_become_established() {
        let mut profiler = profiler();
        let positions = [
            [120.0, 140.0, 420.0, 210.0],
            [980.0, 220.0, 1400.0, 300.0],
            [300.0, 650.0, 900.0, 720.0],
            [1120.0, 760.0, 1700.0, 840.0],
        ];
        let mut prediction = None;
        for (index, bbox) in positions.into_iter().enumerate() {
            prediction = Some(
                profiler
                    .observe_frame(frame(vec![region("bubble", bbox, &format!("Bubble {index}"))]))
                    .unwrap(),
            );
        }
        assert_ne!(prediction.unwrap().mode, "established");
    }

    #[test]
    fn old_profile_json_without_new_fields_still_loads() {
        let json = r#"{
            "schema_version": 1,
            "implementation": "rust_v1",
            "profile_id": "old",
            "frame_count": 1,
            "empty_frame_streak": 0,
            "last_frame_id": "f1",
            "last_timestamp_ms": 1.0,
            "last_frame_width": 1920,
            "last_frame_height": 1080,
            "region_memories": [{
                "id": "region_memory_1",
                "source": "text",
                "mean_bbox": [0.2, 0.7, 0.8, 0.9],
                "count": 1,
                "last_text": "hello",
                "text_change_count": 0,
                "avg_confidence": 0.9
            }],
            "last_prediction": {
                "frame_id": "f1",
                "mode": "exploratory",
                "active_layout_id": null,
                "confidence": 0.4,
                "ocr_regions": [],
                "ignore_regions": [],
                "speaker_regions": [],
                "debug": {}
            }
        }"#;
        let profile: GameProfile = serde_json::from_str(json).unwrap();
        assert_eq!(profile.layout_candidates.len(), 0);
        assert_eq!(profile.region_memories[0].classification, RegionClass::Unknown);
        assert!(profile.last_prediction.unwrap().classified_regions.dialogue.is_empty());
    }

    #[test]
    fn jack_jeanne_like_dialogue_and_bottom_commands_classify_correctly() {
        let mut profiler = profiler();
        let mut prediction = None;
        for _ in 0..4 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![
                        region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                        region("dialogue", [192.0, 549.0, 559.0, 579.0], "見てごらん、日が暮れてるよ。"),
                        region("menu", [520.0, 694.0, 576.0, 708.0], "メニュー"),
                        region("auto", [703.0, 692.0, 792.0, 710.0], "オートプレイ"),
                        region("guide", [1160.0, 694.0, 1254.0, 709.0], "ガイドを閉じる"),
                        region("top_label", [18.0, 25.0, 230.0, 51.0], "Ultrahand"),
                    ]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(!prediction.classified_regions.dialogue.is_empty());
        assert!(!prediction.classified_regions.names.is_empty());
        assert!(!prediction.classified_regions.ui.is_empty());
        assert!(prediction
            .ignore_regions
            .iter()
            .any(|region| region.reason.as_deref() == Some("stable_low_volatility_ui")));
        assert!(prediction.classified_regions.names.iter().all(|region| region.bbox[1] > 400));
        assert!(prediction.classified_regions.ui.iter().any(|region| region.id.contains("cluster")));
    }

    #[test]
    fn bottom_ui_does_not_turn_normal_dialogue_into_overlay() {
        let mut profiler = profiler();
        let mut prediction = None;
        for _ in 0..8 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![
                        region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                        region("dialogue", [192.0, 549.0, 559.0, 579.0], "見てごらん、日が暮れてるよ。"),
                        region("menu", [520.0, 694.0, 576.0, 708.0], "メニュー"),
                        region("auto", [703.0, 692.0, 792.0, 710.0], "オートプレイ"),
                    ]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert_ne!(prediction.ocr_regions[0].purpose.as_deref(), Some("overlay_focus"));
        assert!(profiler
            .profile
            .layout_candidates
            .iter()
            .any(|layout| layout.kind == LayoutKind::NormalDialogue && layout.state == LayoutState::Established));
    }

    #[test]
    fn bottom_control_only_frames_do_not_create_dialogue_layouts() {
        let mut profiler = profiler();
        let mut prediction = None;
        for _ in 0..12 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![region("fast_forward", [830.0, 694.0, 885.0, 709.0], "R早送り")]))
                    .unwrap(),
            );
            profiler.observe_frame(frame_720(Vec::new())).unwrap();
        }

        let prediction = prediction.unwrap();
        assert_eq!(prediction.mode, "fallback");
        assert!(prediction.ocr_regions.is_empty());
        assert!(prediction.ignore_regions.is_empty());
        assert!(prediction.classified_regions.ui.is_empty());
        assert!(profiler.profile.layout_candidates.is_empty());
    }

    #[test]
    fn repeated_ui_text_anywhere_is_excluded_from_dialogue_focus() {
        let mut profiler = profiler();
        let mut prediction = None;
        for index in 0..3 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![
                        region("dialogue", [192.0, 549.0, 760.0, 579.0], &format!("Dialogue line {index}")),
                        region("floating_help", [930.0, 120.0, 1110.0, 146.0], "操作ガイド"),
                    ]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(prediction
            .ignore_regions
            .iter()
            .any(|region| region.reason.as_deref() == Some("stable_low_volatility_ui")));
        assert!(prediction.classified_regions.ui.iter().any(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            x1 <= 930 && y1 <= 120 && x2 >= 1110 && y2 >= 146
        }));
        assert!(prediction.ocr_regions[0].bbox[2] < 900);
    }

    #[test]
    fn repeated_same_dialogue_line_does_not_become_static_ui() {
        let mut profiler = profiler();
        let mut prediction = None;
        for _ in 0..8 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![region(
                        "dialogue",
                        [194.0, 633.0, 453.0, 659.0],
                        "元気になれるもん!",
                    )]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(prediction.classified_regions.ui.is_empty());
        assert!(prediction
            .classified_regions
            .dialogue
            .iter()
            .any(|region| region.reason.as_deref() == Some("volatile_dialogue_like_region")));
    }

    #[test]
    fn explicit_ui_source_is_ignored_after_two_stable_observations() {
        let mut profiler = profiler();
        let mut prediction = None;
        for index in 0..2 {
            prediction = Some(
                profiler
                    .observe_frame(frame_with_ui(
                        vec![region("dialogue", [192.0, 549.0, 760.0, 579.0], &format!("Dialogue line {index}"))],
                        vec![hinted_region("help", [930.0, 120.0, 1110.0, 146.0], "操作ガイド", "ui")],
                    ))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(prediction
            .ignore_regions
            .iter()
            .any(|region| region.reason.as_deref() == Some("stable_low_volatility_ui")));
    }

    #[test]
    fn tiny_command_variants_do_not_become_choice_focus() {
        let mut profiler = profiler();
        let variants = ["で早送り", "き早送り", "早送り"];
        let mut prediction = None;
        for index in 0..6 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![
                        region(
                            "dialogue",
                            [193.0, 551.0, 506.0, 578.0],
                            &format!("あ、うん。またね……。{index}"),
                        ),
                        region("fast_forward", [831.0, 694.0, 884.0, 710.0], variants[index % variants.len()]),
                    ]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(prediction.ocr_regions[0].bbox[3] < 680);
        assert!(prediction.classified_regions.dialogue.iter().all(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            !(x1 <= 831 && y1 <= 694 && x2 >= 884 && y2 >= 710)
        }));
    }

    #[test]
    fn weak_static_ui_is_not_emitted_as_classified_ui() {
        let mut profiler = profiler();
        let prediction = profiler
            .observe_frame(frame_720(vec![
                region("dialogue", [193.0, 551.0, 506.0, 578.0], "あ、うん。またね……。"),
                region("fast_forward", [831.0, 694.0, 884.0, 710.0], "早送り"),
            ]))
            .unwrap();

        assert!(prediction.classified_regions.ui.is_empty());
        assert!(prediction.ignore_regions.is_empty());
    }

    #[test]
    fn stale_ui_is_not_emitted_after_it_stops_appearing() {
        let mut profiler = profiler();
        for index in 0..5 {
            profiler
                .observe_frame(frame_720(vec![
                    region("dialogue", [192.0, 549.0, 760.0, 579.0], &format!("Dialogue line {index}")),
                    region("floating_help", [930.0, 120.0, 1110.0, 146.0], "操作ガイド"),
                ]))
                .unwrap();
        }

        let mut prediction = None;
        for index in 0..12 {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![region(
                        "dialogue",
                        [192.0, 549.0, 760.0, 579.0],
                        &format!("Restarted line {index}"),
                    )]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(profiler
            .profile
            .region_memories
            .iter()
            .any(|cluster| cluster.last_text == "操作ガイド"));
        assert!(prediction.classified_regions.ui.iter().all(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            !(x1 <= 930 && y1 <= 120 && x2 >= 1110 && y2 >= 146)
        }));
        assert!(prediction.ignore_regions.iter().all(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            !(x1 <= 930 && y1 <= 120 && x2 >= 1110 && y2 >= 146)
        }));
    }

    #[test]
    fn high_support_ui_ages_out_of_output_by_last_seen_frame() {
        let mut profiler = profiler();
        for index in 0..20 {
            profiler
                .observe_frame(frame_720(vec![
                    region("dialogue", [192.0, 549.0, 760.0, 579.0], &format!("Dialogue line {index}")),
                    region("floating_help", [930.0, 120.0, 1110.0, 146.0], "操作ガイド"),
                ]))
                .unwrap();
        }

        let mut prediction = None;
        for index in 0..(OUTPUT_RECENT_FRAME_WINDOW + 2) {
            prediction = Some(
                profiler
                    .observe_frame(frame_720(vec![region(
                        "dialogue",
                        [192.0, 549.0, 760.0, 579.0],
                        &format!("Fresh line {index}"),
                    )]))
                    .unwrap(),
            );
        }

        let prediction = prediction.unwrap();
        assert!(profiler
            .profile
            .region_memories
            .iter()
            .any(|cluster| cluster.last_text == "操作ガイド" && cluster.recent_count > 0));
        assert!(prediction.ignore_regions.iter().all(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            !(x1 <= 930 && y1 <= 120 && x2 >= 1110 && y2 >= 146)
        }));
        assert!(prediction.classified_regions.ui.iter().all(|region| {
            let [x1, y1, x2, y2] = region.bbox;
            !(x1 <= 930 && y1 <= 120 && x2 >= 1110 && y2 >= 146)
        }));
    }

    #[test]
    fn jittered_dialogue_fragments_match_same_layout_by_focus_geometry() {
        let mut profiler = profiler();
        let fragment_sets = [
            vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region("dialogue_left", [192.0, 549.0, 520.0, 579.0], "左側の台詞"),
                region("dialogue_right", [680.0, 549.0, 1050.0, 579.0], "右側の台詞"),
            ],
            vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region("dialogue_mid", [360.0, 549.0, 760.0, 579.0], "中央の台詞"),
                region("dialogue_right", [720.0, 549.0, 1120.0, 579.0], "続きの台詞"),
            ],
            vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region("dialogue_left", [192.0, 549.0, 590.0, 579.0], "また左側"),
                region("dialogue_mid", [600.0, 549.0, 980.0, 579.0], "また中央"),
            ],
            vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region("dialogue_left", [210.0, 549.0, 560.0, 579.0], "形が少し変わる"),
                region("dialogue_right", [700.0, 549.0, 1090.0, 579.0], "でも同じ帯"),
            ],
        ];

        let mut prediction = None;
        for regions in fragment_sets {
            prediction = Some(profiler.observe_frame(frame_720(regions)).unwrap());
        }

        let prediction = prediction.unwrap();
        assert_eq!(prediction.mode, "established");
        assert_eq!(
            profiler
                .profile
                .layout_candidates
                .iter()
                .filter(|layout| layout.kind == LayoutKind::NormalDialogue)
                .count(),
            1
        );
    }

    #[test]
    fn established_layout_prediction_expands_to_current_dialogue_shape() {
        let mut profiler = profiler();
        for _ in 0..4 {
            profiler
                .observe_frame(frame_720(vec![
                    region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                    region("dialogue", [192.0, 549.0, 520.0, 579.0], "短い台詞"),
                ]))
                .unwrap();
        }

        let prediction = profiler
            .observe_frame(frame_720(vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region(
                    "dialogue",
                    [192.0, 549.0, 1050.0, 579.0],
                    "これは横に長く伸びた台詞なので切れてはいけない",
                ),
            ]))
            .unwrap();

        assert!(prediction.ocr_regions[0].bbox[2] >= 1050);
    }

    #[test]
    fn established_dialogue_layout_keeps_learned_envelope_on_short_text() {
        let mut profiler = profiler();
        for index in 0..5 {
            profiler
                .observe_frame(frame_720(vec![
                    region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                    region("dialogue_top", [192.0, 549.0, 1050.0, 579.0], &format!("長い一行目 {index}")),
                    region("dialogue_bottom", [194.0, 591.0, 760.0, 620.0], &format!("二行目 {index}")),
                ]))
                .unwrap();
        }

        let prediction = profiler
            .observe_frame(frame_720(vec![
                region("speaker", [147.0, 482.0, 265.0, 509.0], "立花継希"),
                region("dialogue_short", [192.0, 549.0, 390.0, 579.0], "短い"),
            ]))
            .unwrap();

        let bbox = prediction.ocr_regions[0].bbox;
        assert_eq!(prediction.mode, "established");
        assert!(bbox[2] >= 1050);
        assert!(bbox[3] >= 620);
    }

    #[test]
    fn new_recent_ui_is_returned_after_old_ui_has_confidence() {
        let mut profiler = profiler();
        for index in 0..4 {
            let mut ui = (0..10)
                .map(|button| {
                    hinted_region(
                        &format!("old_ui_{button}"),
                        [20.0 + button as f64 * 80.0, 690.0, 70.0 + button as f64 * 80.0, 710.0],
                        &format!("Old{button}"),
                        "ui",
                    )
                })
                .collect::<Vec<_>>();
            if index >= 1 {
                ui.push(hinted_region("new_ui", [1040.0, 690.0, 1190.0, 710.0], "操作ガイド", "ui"));
            }
            profiler
                .observe_frame(frame_with_ui(
                    vec![region("dialogue", [192.0, 549.0, 760.0, 579.0], &format!("Line {index}"))],
                    ui,
                ))
                .unwrap();
        }

        let prediction = profiler
            .observe_frame(frame_with_ui(
                vec![region("dialogue", [192.0, 549.0, 760.0, 579.0], "Line final")],
                vec![hinted_region("new_ui", [1040.0, 690.0, 1190.0, 710.0], "操作ガイド", "ui")],
            ))
            .unwrap();

        assert!(prediction.ignore_regions.iter().any(|region| {
            let [x1, _, x2, _] = region.bbox;
            x1 <= 1040 && x2 >= 1190
        }));
    }
}
