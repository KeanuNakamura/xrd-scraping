## Setup

### 1. Start GROBID with Docker

Run the GROBID server in a Docker container:

```bash
docker run --rm \
  --init \
  --ulimit core=0 \
  -p 8070:8070 \
  grobid/grobid:0.9.0-crf
```

GROBID will be available at:

```text
http://localhost:8070
```

Keep this terminal open while parsing PDFs.

### 2. Create a Virtual Environment

Create the Python virtual environment:

```bash
python3 -m venv .venv
```

Activate it on macOS or Linux:

```bash
source .venv/bin/activate
```

Activate it on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies

Upgrade `pip` and install the required packages:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

Place the PDF you want to parse inside the `pdf_files/` directory.

Run the parser using the following format:

```bash
python pdf_parser.py <pdf_path> --output <output_path>
```

### Example

```bash
python pdf_parser.py pdf_files/tio2_powder.pdf \
  --output grobid_output/tio2_powder
```

The parser will process the PDF using the local GROBID server and save the generated files to the specified output directory.

## Example Project Structure

```text
project/
├── pdf_files/
│   └── tio2_powder.pdf
├── grobid_output/
├── pdf_parser.py
├── requirements.txt
├── .gitignore
└── README.md
```

## Stopping GROBID

To stop the GROBID Docker container, press `Ctrl+C` in the terminal where it is running.
