use serde::{Deserialize, Serialize};

pub type BBox = [f64; 4];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfileOpenOptions {
    pub profile_id: String,
    pub profile_path: String,
    #[serde(default)]
    pub flush_policy: FlushPolicy,
    #[serde(default)]
    pub config: ProfilerConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum FlushPolicy {
    EveryObservation,
    EveryNObservations { n: u64 },
    Manual,
}

impl Default for FlushPolicy {
    fn default() -> Self {
        FlushPolicy::EveryNObservations { n: 25 }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfilerConfig {
    #[serde(default = "default_min_region_confidence")]
    pub min_region_confidence: f64,
    #[serde(default = "default_prediction_padding_ratio")]
    pub prediction_padding_ratio: f64,
    #[serde(default = "default_fallback_after_empty_frames")]
    pub fallback_after_empty_frames: u64,
    #[serde(default = "default_established_after_observations")]
    pub established_after_observations: u64,
    #[serde(default = "default_exploratory_after_observations")]
    pub exploratory_after_observations: u64,
    #[serde(default = "default_ignore_static_after_observations")]
    pub ignore_static_after_observations: u64,
    #[serde(default = "default_max_ignore_regions")]
    pub max_ignore_regions: u64,
    #[serde(default = "default_cluster_match_iou")]
    pub cluster_match_iou: f64,
    #[serde(default = "default_cluster_match_center_distance")]
    pub cluster_match_center_distance: f64,
    #[serde(default = "default_layout_match_overlap")]
    pub layout_match_overlap: f64,
    #[serde(default = "default_layout_established_after_matches")]
    pub layout_established_after_matches: u64,
    #[serde(default = "default_tentative_layout_expire_after_frames")]
    pub tentative_layout_expire_after_frames: u64,
}

impl Default for ProfilerConfig {
    fn default() -> Self {
        Self {
            min_region_confidence: default_min_region_confidence(),
            prediction_padding_ratio: default_prediction_padding_ratio(),
            fallback_after_empty_frames: default_fallback_after_empty_frames(),
            established_after_observations: default_established_after_observations(),
            exploratory_after_observations: default_exploratory_after_observations(),
            ignore_static_after_observations: default_ignore_static_after_observations(),
            max_ignore_regions: default_max_ignore_regions(),
            cluster_match_iou: default_cluster_match_iou(),
            cluster_match_center_distance: default_cluster_match_center_distance(),
            layout_match_overlap: default_layout_match_overlap(),
            layout_established_after_matches: default_layout_established_after_matches(),
            tentative_layout_expire_after_frames: default_tentative_layout_expire_after_frames(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrameObservation {
    pub frame_id: Option<String>,
    pub timestamp_ms: Option<f64>,
    pub frame: FrameInfo,
    #[serde(default)]
    pub regions: Vec<RegionObservation>,
    #[serde(default)]
    pub ui_regions: Vec<RegionObservation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrameInfo {
    pub width: u32,
    pub height: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegionObservation {
    pub id: Option<String>,
    pub bbox: [f64; 4],
    #[serde(default)]
    pub text: String,
    #[serde(default = "default_confidence")]
    pub confidence: f64,
    #[serde(default)]
    pub is_vertical: bool,
    #[serde(default)]
    pub chars: Vec<CharObservation>,
    #[serde(default)]
    pub kind_hint: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CharObservation {
    #[serde(rename = "char", alias = "ch")]
    pub value: String,
    pub bbox: [f64; 4],
    pub confidence: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LayoutPrediction {
    pub frame_id: Option<String>,
    pub mode: String,
    pub active_layout_id: Option<String>,
    pub confidence: f64,
    pub ocr_regions: Vec<PredictedRegion>,
    pub ignore_regions: Vec<PredictedRegion>,
    pub speaker_regions: Vec<PredictedRegion>,
    #[serde(default)]
    pub classified_regions: ClassifiedRegions,
    pub debug: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PredictedRegion {
    pub id: String,
    pub bbox: [i64; 4],
    pub confidence: f64,
    pub purpose: Option<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ClassifiedRegions {
    pub dialogue: Vec<PredictedRegion>,
    pub names: Vec<PredictedRegion>,
    pub ui: Vec<PredictedRegion>,
    pub non_dialogue: Vec<PredictedRegion>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GameProfile {
    pub schema_version: u64,
    pub implementation: String,
    pub profile_id: String,
    pub frame_count: u64,
    pub empty_frame_streak: u64,
    pub last_frame_id: Option<String>,
    pub last_timestamp_ms: Option<f64>,
    pub last_frame_width: Option<u32>,
    pub last_frame_height: Option<u32>,
    #[serde(default)]
    pub region_memories: Vec<RegionCluster>,
    #[serde(default)]
    pub layout_candidates: Vec<LayoutCandidate>,
    pub last_prediction: Option<LayoutPrediction>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegionCluster {
    pub id: String,
    #[serde(default)]
    pub source: String,
    pub mean_bbox: BBox,
    #[serde(default)]
    pub variance_bbox: BBox,
    pub count: u64,
    #[serde(default)]
    pub recent_count: u64,
    #[serde(default)]
    pub last_seen_frame: u64,
    pub last_text: String,
    pub text_change_count: u64,
    #[serde(default)]
    pub unique_text_count: u64,
    #[serde(default)]
    pub repeated_text_count: u64,
    #[serde(default)]
    pub empty_text_count: u64,
    #[serde(default)]
    pub text_len_sum: u64,
    #[serde(default)]
    pub text_len_square_sum: u64,
    pub avg_confidence: f64,
    #[serde(default)]
    pub vertical_count: u64,
    #[serde(default)]
    pub text_source_count: u64,
    #[serde(default)]
    pub ui_source_count: u64,
    #[serde(default)]
    pub role_hint_counts: RoleHintCounts,
    #[serde(default)]
    pub classification: RegionClass,
    #[serde(default)]
    pub confidence: f64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RoleHintCounts {
    pub dialogue: u64,
    pub speaker: u64,
    pub ui: u64,
    pub choice: u64,
    pub system: u64,
    pub decorative: u64,
    pub unknown: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RegionClass {
    Dialogue,
    Name,
    StaticUi,
    Choice,
    BacklogText,
    OverlayFocus,
    DecorativeOrNoise,
    #[default]
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LayoutCandidate {
    pub id: String,
    #[serde(default)]
    pub kind: LayoutKind,
    #[serde(default)]
    pub state: LayoutState,
    #[serde(default)]
    pub cluster_ids: Vec<String>,
    #[serde(default)]
    pub dialogue_cluster_ids: Vec<String>,
    #[serde(default)]
    pub name_cluster_ids: Vec<String>,
    #[serde(default)]
    pub ui_cluster_ids: Vec<String>,
    #[serde(default)]
    pub support_count: u64,
    #[serde(default)]
    pub recent_support: u64,
    #[serde(default)]
    pub first_seen_frame: u64,
    #[serde(default)]
    pub last_seen_frame: u64,
    #[serde(default)]
    pub confidence: f64,
    #[serde(default)]
    pub focus_bbox: Option<BBox>,
    #[serde(default)]
    pub occluded_base_layout_id: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LayoutKind {
    NormalDialogue,
    ChoiceMenu,
    NarrationOrSystem,
    Backlog,
    BattleDialogue,
    OverlayPanel,
    #[default]
    Unknown,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LayoutState {
    #[default]
    Tentative,
    Established,
    Retired,
}

pub fn normalize_bbox(bbox: [f64; 4], width: u32, height: u32) -> BBox {
    let x1 = bbox[0].min(bbox[2]);
    let y1 = bbox[1].min(bbox[3]);
    let x2 = bbox[0].max(bbox[2]);
    let y2 = bbox[1].max(bbox[3]);
    [
        clamp01(x1 / width as f64),
        clamp01(y1 / height as f64),
        clamp01(x2 / width as f64),
        clamp01(y2 / height as f64),
    ]
}

pub fn denormalize_bbox(bbox: BBox, width: u32, height: u32) -> [i64; 4] {
    [
        (clamp01(bbox[0]) * width as f64).round() as i64,
        (clamp01(bbox[1]) * height as f64).round() as i64,
        (clamp01(bbox[2]) * width as f64).round() as i64,
        (clamp01(bbox[3]) * height as f64).round() as i64,
    ]
}

pub fn union_bbox(boxes: &[BBox]) -> Option<BBox> {
    if boxes.is_empty() {
        return None;
    }
    Some([
        boxes.iter().map(|bbox| bbox[0]).fold(f64::INFINITY, f64::min),
        boxes.iter().map(|bbox| bbox[1]).fold(f64::INFINITY, f64::min),
        boxes.iter().map(|bbox| bbox[2]).fold(f64::NEG_INFINITY, f64::max),
        boxes.iter().map(|bbox| bbox[3]).fold(f64::NEG_INFINITY, f64::max),
    ])
}

pub fn expand_bbox(bbox: BBox, padding: f64) -> BBox {
    [
        clamp01(bbox[0] - padding),
        clamp01(bbox[1] - padding),
        clamp01(bbox[2] + padding),
        clamp01(bbox[3] + padding),
    ]
}

pub fn bbox_area(bbox: BBox) -> f64 {
    (bbox[2] - bbox[0]).max(0.0) * (bbox[3] - bbox[1]).max(0.0)
}

pub fn bbox_center(bbox: BBox) -> (f64, f64) {
    ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
}

pub fn center_distance(a: BBox, b: BBox) -> f64 {
    let (ax, ay) = bbox_center(a);
    let (bx, by) = bbox_center(b);
    ((ax - bx).powi(2) + (ay - by).powi(2)).sqrt()
}

pub fn iou(a: BBox, b: BBox) -> f64 {
    let intersection = bbox_area([
        a[0].max(b[0]),
        a[1].max(b[1]),
        a[2].min(b[2]),
        a[3].min(b[3]),
    ]);
    let denom = bbox_area(a) + bbox_area(b) - intersection;
    if denom <= 0.0 {
        0.0
    } else {
        intersection / denom
    }
}

fn clamp01(value: f64) -> f64 {
    value.clamp(0.0, 1.0)
}

fn default_confidence() -> f64 {
    1.0
}

fn default_min_region_confidence() -> f64 {
    0.25
}

fn default_prediction_padding_ratio() -> f64 {
    0.025
}

fn default_fallback_after_empty_frames() -> u64 {
    4
}

fn default_established_after_observations() -> u64 {
    20
}

fn default_exploratory_after_observations() -> u64 {
    3
}

fn default_ignore_static_after_observations() -> u64 {
    12
}

fn default_max_ignore_regions() -> u64 {
    24
}

fn default_cluster_match_iou() -> f64 {
    0.45
}

fn default_cluster_match_center_distance() -> f64 {
    0.05
}

fn default_layout_match_overlap() -> f64 {
    0.55
}

fn default_layout_established_after_matches() -> u64 {
    12
}

fn default_tentative_layout_expire_after_frames() -> u64 {
    600
}
