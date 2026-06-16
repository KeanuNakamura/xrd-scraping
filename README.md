Run on docker: 
docker run --rm \
 --init \
 --ulimit core=0 \
 -p 8070:8070 \
 grobid/grobid:0.9.0-crf

Virtual Env:
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Parse PDFs with the following format: 
python pdf_parser.py pdf_files/paper.pdf --output grobid_output/paper
Example: python pdf_parser.py pdf_files/ti02_powder.pdf --output grobid_output/ti02_powder
