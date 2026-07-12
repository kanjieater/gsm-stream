# GSM OCR Area Config — Exclusion Zones Reference

Not implemented yet. Use text-frequency suppression (`bridge.py`) instead. This doc covers the GSM-native approach for if that proves insufficient.

## What it is

GSM's desktop app has a Qt-based area selector that draws orange exclusion boxes on the screen. These get saved as JSON configs and are applied by `apply_ocr_config_to_image()` before OCR runs — filling excluded regions with background color so neither meikiocr nor glens sees the UI.

## Data structures (in GSM package)

```python
# GameSentenceMiner/ocr/gsm_ocr_config.py

@dataclass_json
@dataclass
class Rectangle:
    monitor: Monitor
    coordinates: List[Union[float, int]]  # [x, y, width, height]
    is_excluded: bool                      # True = orange exclusion box
    is_secondary: bool = False
    is_exclusive: bool = False
    is_black_hole: bool = False

@dataclass_json
@dataclass
class OCRConfig:
    scene: str
    rectangles: List[Rectangle]
    coordinate_system: str = None  # "percentage" (0.0–1.0) or absolute pixels
    window_geometry: Optional[WindowGeometry] = None
    window: Optional[str] = None
    language: str = "ja"
```

## How masking works

`apply_ocr_config_to_image(img, ocr_config, return_full_size=True)` in `owocr/ocr_runtime.py` (line 2893):
- Iterates `ocr_config.rectangles`
- For each `is_excluded=True` rectangle: fills with `_get_rectangle_mask_fill(img)` (background color)
- Returns the modified PIL image

This can be called directly in bridge.py before passing frames to meikiocr or glens.

## Config file format

```json
{
  "scene": "jack_jeanne",
  "coordinate_system": "percentage",
  "language": "ja",
  "rectangles": [
    {
      "monitor": {"index": 0},
      "coordinates": [0.0, 0.87, 1.0, 0.13],
      "is_excluded": true,
      "is_secondary": false,
      "is_exclusive": false,
      "is_black_hole": false
    }
  ]
}
```

Coordinates are `[x, y, width, height]` as fractions (0.0–1.0) when `coordinate_system = "percentage"`.

## Headless Docker limitation

The Qt GUI area selector (`owocr_area_selector_qt.py`) requires a display server — won't run in Docker.

To use this approach, you'd need either:
1. A browser-based canvas UI added to bridge.py (Flask route `/exclusion-config`)
2. Or hand-edit the JSON files in the mounted volume

The aiohttp gateway at port 7275 forwards all HTTP requests to Flask, so adding new Flask routes works and they're accessible at `gsm.<your-domain>/<route>` through NPM.

## Implementation sketch (if needed)

```python
from GameSentenceMiner.ocr.gsm_ocr_config import OCRConfig
from GameSentenceMiner.owocr.owocr.ocr_runtime import apply_ocr_config_to_image

def _apply_exclusions(pil: Image.Image, profile_name: str) -> Image.Image:
    path = f"/app/data/profiles/{profile_name}.json"
    if not os.path.exists(path):
        return pil
    with open(path) as f:
        cfg = OCRConfig.from_dict(json.load(f))
    if not any(r.is_excluded for r in cfg.rectangles):
        return pil
    result, _ = apply_ocr_config_to_image(pil.copy(), cfg, return_full_size=True)
    return result
```

Profile switching requires knowing the active game — since RTSP provides no game metadata, profiles would need manual selection (e.g., a `/exclusion-config/active` POST endpoint).
