# XRD PDF Scraping

Extract figures, captions, and surrounding context from scientific PDFs, then enrich each crop with structured vision analysis. The default path stops at scrape + metadata — curve digitization is an optional bonus.

```
PDF → GROBID parse → figure crops + captions + context
    → mini XRD classify → full gpt-4.1 analysis (XRD/uncertain only)
    → <stem>.figure_analysis.json
    → (optional) --digitize → calibrated CSV / preview PNGs
    → (optional) --paper-analysis → paper_analysis synthesis
```

---

### 1. Install Docker

- macOS / Windows: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
- Linux: install Docker Engine and the Compose plugin, then confirm with `docker compose version`.

### 2. Get the Compose file and folders

Either clone the repo, or download only [`compose.yaml`](compose.yaml) into an empty working directory:

```bash
mkdir -p xrd-work && cd xrd-work
# If you cloned the repo, you already have compose.yaml.
# Otherwise download it, then:
mkdir -p papers results
```

Put the PDF(s) you want to process into `papers/`.

### 3. Pull images and start GROBID

```bash
docker pull keanu375/xrd-pipeline:v3
docker pull grobid/grobid:0.9.0-crf

docker compose up -d grobid
```

GROBID needs ~30–60s to become healthy on first start. You can confirm with:

```bash
curl http://localhost:8070/api/isalive
# expect: true
```

`docker compose run --rm xrd` also waits until GROBID is healthy before starting.

### 4. Set your OpenAI API key

```bash
export OPENAI_API_KEY=sk-... 
```

### 5. Process PDFs

Process every PDF under `papers/` and write results to `results/`:

```bash
docker compose run --rm xrd
```

### Expected output

For each PDF, the pipeline writes `results/<stem>/`:

| Path | Contents |
|------|----------|
| `xrd_figures/<id>.png` | Crops classified as XRD-related |
| `non_xrd_figures/<id>.png` | Crops classified as not XRD |
| `<stem>.figure_analysis.json` | Per-figure path, caption, paper context, and `analysis` metadata |
| `extra/` | Original PDF plus GROBID TEI / parse records |

Digitization is **off** unless you pass `--digitize` (then XRD crops are triaged under `xrd_figures/` and CSVs / preview PNGs are added).

Stop GROBID when finished: `docker compose down`.

### Alternative: `docker run` (no Compose file)

Use this when GROBID is published on the host (`-p 8070:8070`). Containers reach the host via `host.docker.internal` (Docker Desktop; on Linux add `--add-host=host.docker.internal:host-gateway`).

Compose and this manual path both bind host port **8070** and the manual container uses `--name grobid`, so only one GROBID should be running at a time. Free a previous run first if needed:

```bash
docker compose down          # stops Compose's xrd_scraping-grobid-1
docker rm -f grobid          # removes a leftover manual container named grobid
```

Start GROBID (foreground — Ctrl+C stops and removes it; use a second terminal for the pipeline, or add `-d` to detach):

```bash
docker run --rm \
  --init \
  --ulimit core=0 \
  --name grobid \
  -p 8070:8070 \
  grobid/grobid:0.9.0-crf
```

Then process PDFs:

```bash
mkdir -p papers results
# copy PDFs into ./papers

# wait until: curl http://localhost:8070/api/isalive → true

docker run --rm \
  -e OPENAI_API_KEY \
  -v "$PWD/papers:/app/papers:ro" \
  -v "$PWD/results:/app/results" \
  --add-host=host.docker.internal:host-gateway \
  keanu375/xrd-pipeline:v3 \
  python scripts/scrape_and_digitize.py papers/ results/ \
  --grobid-url http://host.docker.internal:8070
```

**Networking:** with Docker Compose, use the service hostname `http://grobid:8070`. With separate `docker run` containers talking through the host port, use `http://host.docker.internal:8070`.

### Optional: Kubernetes

Same I/O as Compose: PDFs in `papers/`, results in `results/`. Prefer Compose for day-to-day local use; use this path for batch / cluster runs (Docker Desktop Kubernetes, kind, or minikube). Requires a local clone of the repo (manifests live under [`k8s/`](k8s/)).

**Prerequisites**

- `kubectl` pointed at a local cluster with a default StorageClass (Docker Desktop / kind / minikube)
- `envsubst` (macOS: `brew install gettext`)
- Image `keanu375/xrd-pipeline:v3` available to the cluster (`docker pull` is enough on Docker Desktop)

**1. Put PDFs in `papers/`**

```bash
mkdir -p papers results
# copy PDFs into ./papers
```

**2. Deploy GROBID**

```bash
kubectl apply -k k8s/
kubectl rollout status deployment/grobid --timeout=180s
```

**3. Create the OpenAI secret**

```bash
export OPENAI_API_KEY=sk-...
kubectl create secret generic xrd-openai \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
```

**4. Run the pipeline**

```bash
./k8s/run.sh
```

`k8s/run.sh` stages host `./papers` into PVC `xrd-data`, runs an Indexed Job (one PDF per pod) against GROBID, then copies `results/<stem>/` back to the host (same layout as Compose). Override paths with `XRD_PAPERS` / `XRD_RESULTS` if needed.

Docker Desktop Kubernetes cannot mount macOS `/Users` via `hostPath`, so the PVC staging step is required there.

**Cleanup**

```bash
kubectl delete job xrd-process --ignore-not-found
kubectl delete pod xrd-stage --ignore-not-found
kubectl delete pvc xrd-data --ignore-not-found
kubectl delete -k k8s/
kubectl delete secret xrd-openai --ignore-not-found
```

### Output shape

Each entry in `<stem>.figure_analysis.json` looks like:

```json
{
  "figure": 1,
  "figure_path": "figures/fig_1.png",
  "caption": "Figure 1. XRD spectra as a function of …",
  "text": "…surrounding paper context…",
  "analysis": {
    "is_xrd": true,
    "sample": "…",
    "composition": ["TiO2"],
    "space_group": ["P42/mnm"],
    "curves": [{
      "condition": "…",
      "composition": ["TiO2"],
      "space_group": ["P42/mnm"],
      "peak_positions": [],
      "phases": []
    }],
    "trends": ["…"]
  }
}
```

`composition` and `space_group` are literature-only lists (empty unless the paper states them). They appear at both figure and curve level.

Paper-level synthesis is **opt-in** (`--paper-analysis`) and stored as `paper_analysis`.

See [`examples/scrape/`](examples/scrape/) for sample XRD crops and a portable JSON sketch, and [`examples/DEMO.md`](examples/DEMO.md) for a walkthrough.

### Useful flags

| Flag | Effect |
|------|--------|
| `--grobid-url URL` | GROBID base URL (default `http://localhost:8070`) |
| `--figure-dpi N` | Render DPI for crops (default `300`) |
| `--analysis-model NAME` | Full enrichment model (default `gpt-4.1`; alias: `--model`) |
| `--triage-model NAME` | Classify/triage model (default `gpt-4.1-mini`) |
| `--paper-analysis` | Run optional paper-level XRD synthesis |
| `--xrd-figures-only` | Optional GROBID XRD prefilter (off by default) |
| `--overwrite` | Replace an existing `output/<stem>/` |
| `--benchmark` | Crop all figures, classify XRD vs not, copy into global `xrd/` / `not_xrd/` (no digitize) |
| `--digitize` | After scrape+analysis, triage and digitize XRD curves (reuses classify) |
| `--all-figures` | With `--digitize`: digitize every crop, not only XRD-classified ones |

```bash
# scrape + two-tier analysis
python scripts/scrape_and_digitize.py paper.pdf output/

# + paper synthesis
python scripts/scrape_and_digitize.py paper.pdf output/ --paper-analysis

# + digitize (reuses enrichment classification)
python scripts/scrape_and_digitize.py paper.pdf output/ --digitize
```

### Analyze already-cropped figures

```bash
export OPENAI_API_KEY=...
python scripts/analyze_figures.py path/to/figure_pngs/ output/figure_metadata/
```

## Demo

The nanosized-powders paper mixes TEM, SAXS, and XRD. After scrape + analysis, the XRD-related crops (e.g. figures 1 and 5) land under `xrd_figures/` with captions and context in `figure_analysis.json`:

<table>
  <tr>
    <td align="center"><strong>Figure 1</strong><br/><img src="examples/scrape/fig_1.png" alt="Scraped XRD figure 1" width="100%"/></td>
    <td align="center"><strong>Figure 5</strong><br/><img src="examples/scrape/fig_5.png" alt="Scraped XRD figure 5" width="100%"/></td>
  </tr>
</table>

Full walkthrough: [`examples/DEMO.md`](examples/DEMO.md).

---

## Bonus: curve digitization

Optional path that turns a single-curve (or stacked shared-x) XRD PNG into calibrated `(x, y)` CSV plus a preview PNG. Needs [Tesseract](https://github.com/tesseract-ocr/tesseract) for axis OCR (`brew install tesseract` on macOS). `CLIPDROP_API_KEY` is only required when in-plot text must be removed. The published Docker image already includes Tesseract.

### End-to-end with scrape

```bash
export OPENAI_API_KEY=...
export CLIPDROP_API_KEY=...   # only if a figure needs ClipDrop

python scripts/scrape_and_digitize.py paper.pdf output/ --digitize
```

With Docker Compose:

```bash
docker compose run --rm -e CLIPDROP_API_KEY xrd python scripts/scrape_and_digitize.py \
  papers/ results/ --grobid-url http://grobid:8070 --digitize
```

With `--digitize`, crops under `xrd_figures/` are triaged and digitized using the stored XRD classification (use `--all-figures` to also digitize `non_xrd_figures/`).

### Digitize one PNG

```bash
python scripts/digitize_figure.py examples/figure_3.png output/
# → output/figure_3/figure_3.csv
# → output/figure_3/figure_3_digitized.png
```

Add `--clipdrop` to remove in-plot text before tracing (uses ClipDrop credits).

### Digitization examples

CNRS figure 1034 — original, reconstructed curve, overlay (SID ≈ 1.02):

<table>
  <tr>
    <td align="center" width="33%"><strong>Original</strong></td>
    <td align="center" width="33%"><strong>Digitized</strong></td>
    <td align="center" width="33%"><strong>Overlay</strong></td>
  </tr>
  <tr>
    <td align="center"><img src="examples/figure_1034_original.png" alt="Original XRD figure 1034" width="100%"/></td>
    <td align="center"><img src="examples/figure_1034_digitized.png" alt="Digitized XRD curve for figure 1034" width="100%"/></td>
    <td align="center"><img src="examples/figure_1034_overlay.png" alt="Overlay of original vs digitized figure 1034" width="100%"/></td>
  </tr>
</table>

CNRS figure 1051 — original, reconstructed curve, overlay (SID ≈ 0.68):

<table>
  <tr>
    <td align="center" width="33%"><strong>Original</strong></td>
    <td align="center" width="33%"><strong>Digitized</strong></td>
    <td align="center" width="33%"><strong>Overlay</strong></td>
  </tr>
  <tr>
    <td align="center"><img src="examples/figure_1051_original.png" alt="Original XRD figure 1051" width="100%"/></td>
    <td align="center"><img src="examples/figure_1051_digitized.png" alt="Digitized XRD curve for figure 1051" width="100%"/></td>
    <td align="center"><img src="examples/figure_1051_overlay.png" alt="Overlay of original vs digitized figure 1051" width="100%"/></td>
  </tr>
</table>

#### Figures with in-plot text

With `--clipdrop`, in-plot labels are cleared, then the cleaned interior is pasted back onto the original axes so tick OCR still works:

<table>
  <tr>
    <td align="center" width="25%"><strong>Original</strong></td>
    <td align="center" width="25%"><strong>Clean</strong></td>
    <td align="center" width="25%"><strong>Axes restored</strong></td>
    <td align="center" width="25%"><strong>Digitized</strong></td>
  </tr>
  <tr>
    <td align="center"><img src="examples/figure_8_original.png" alt="Original figure 8 with Miller index labels on peaks" width="100%"/></td>
    <td align="center"><img src="examples/figure_8_clean.png" alt="ClipDrop-cleaned plot interior" width="100%"/></td>
    <td align="center"><img src="examples/figure_8_axes_restored.png" alt="Cleaned interior on original axes" width="100%"/></td>
    <td align="center"><img src="examples/figure_8_digitized.png" alt="Digitized calibrated XRD curve for figure 8" width="100%"/></td>
  </tr>
</table>

> Monshi, A., Foroughi, M. R., & Monshi, M. R. (2012). Modified Scherrer Equation to Estimate More Accurately Nano-Crystallite Size Using XRD. *World Journal of Nano Science and Engineering*, *2*, 154–160. https://doi.org/10.4236/wjnse.2012.23020

### Spectral Information Divergence (SID)

SID scores digitized spectra against ground truth (`compute_sid.py`). Lower is better; overlays report raw/modified SID, peak F1, and a final XRD score written to `<figure_id>_scores.json` / `sid_summary.json`.
