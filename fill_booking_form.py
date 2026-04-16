#!/usr/bin/env python3
"""
Orthofix UK Loan Booking Form — PDF Filler Service
Receives JSON from n8n, fills the PDF, returns base64-encoded result.

Environment variables:
  PDF_TEMPLATE  : path to the blank booking form PDF (default: UK_Loan_Booking_Form.pdf)
  API_KEY       : secret key for X-API-Key header authentication
  PORT          : port to listen on (default: 5000)

Dependencies:
  pip install flask pypdf pdfplumber
"""

from flask import Flask, request, jsonify, abort
from pypdf import PdfReader, PdfWriter
import base64, io, os, logging
from functools import lru_cache

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

PDF_TEMPLATE = os.environ.get('PDF_TEMPLATE', 'UK_Loan_Booking_Form.pdf')
API_KEY      = os.environ.get('API_KEY', 'changeme')

# ── Header field map (payload key → PDF field ID) ─────────────────────────────

HEADER_MAP = {
    'tm_contact':             'TM Contact',
    'po_number':              'Text1',
    'hospital':               'Text2',
    'delivery_address':       'Delivery Address',
    'hospital_contact_name':  'Text3',
    'hospital_contact_phone': 'Text4',
    'surgery_date':           'Date5_af_date',
    'surgeon_name':           'Text8',
    'delivery_date':          'Date6_af_date',
    'delivery_time':          'Dropdown9',
    'collection_date':        'Date7_af_date',
    'collection_time':        'Dropdown10',
    'comments':               'Additional Kit  Comments',
}

# Fields that are NOT product quantity fields
_SKIP = {'TM Contact', 'Delivery Address', 'Additional Kit  Comments', 'Button11', 'Button12'}

def _is_header(name):
    return name in _SKIP or name.startswith(('Text', 'Date', 'Dropdown'))


# ── Build product index at startup ────────────────────────────────────────────

@lru_cache(maxsize=1)
def _build_product_index(pdf_path):
    """
    Returns an ordered list of (field_id, page_index) tuples, one per product row,
    sorted by visual position on the form (top of page 1 → bottom of last page).
    Index 0 = product number 1, index 1 = product number 2, etc.
    """
    reader = PdfReader(pdf_path)
    entries = []

    for page_idx, page in enumerate(reader.pages):
        page_h = float(page.mediabox[3])
        annots_ref = page.get('/Annots')
        if not annots_ref:
            continue
        annots = annots_ref.get_object() if hasattr(annots_ref, 'get_object') else annots_ref
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object() if hasattr(annot_ref, 'get_object') else annot_ref
                if annot.get('/Subtype') != '/Widget':
                    continue
                field_id = str(annot.get('/T', ''))
                if _is_header(field_id):
                    continue
                rect = [float(x) for x in annot.get('/Rect', [0, 0, 0, 0])]
                y_top = rect[3]  # higher value = higher on page in PDF coords
                entries.append((page_idx, y_top, field_id))
            except Exception:
                pass

    # Sort: page ascending, then y descending (top of page first)
    entries.sort(key=lambda e: (e[0], -e[1]))

    log.info(f"Product index built: {len(entries)} fields mapped")
    return [(field_id, page_idx) for (page_idx, _, field_id) in entries]


# ── Core fill logic ───────────────────────────────────────────────────────────

def fill_pdf(payload: dict) -> bytes:
    product_index = _build_product_index(PDF_TEMPLATE)

    reader = PdfReader(PDF_TEMPLATE)
    writer = PdfWriter()
    writer.append(reader)

    # --- 1. Fill header fields (all on page 0) ---
    header_values = {}
    for key, field_id in HEADER_MAP.items():
        value = payload.get(key, '')
        if value:
            header_values[field_id] = str(value)

    if header_values:
        writer.update_page_form_field_values(
            writer.pages[0], header_values, auto_regenerate=False
        )

    # --- 2. Fill kit quantity fields ---
    # Build a dict of page_index → {field_id: qty} to batch updates per page
    page_updates: dict[int, dict] = {}

    for item in payload.get('kit', []):
        product_number = item.get('product_number')
        qty = str(item.get('qty', '1'))

        if product_number is None:
            log.warning(f"Kit item missing product_number: {item}")
            continue

        idx = int(product_number) - 1  # product numbers are 1-based

        if idx < 0 or idx >= len(product_index):
            log.warning(f"product_number {product_number} out of range (max {len(product_index)})")
            continue

        field_id, page_idx = product_index[idx]

        if page_idx not in page_updates:
            page_updates[page_idx] = {}
        page_updates[page_idx][field_id] = qty

    for page_idx, updates in page_updates.items():
        writer.update_page_form_field_values(
            writer.pages[page_idx], updates, auto_regenerate=False
        )
        log.info(f"Page {page_idx + 1}: wrote {len(updates)} kit field(s)")

    # --- 3. Write to buffer ---
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_filename(payload: dict) -> str:
    hospital = payload.get('hospital', 'Unknown').replace(' ', '_').replace(',', '')[:30]
    date = payload.get('surgery_date', 'Unknown').replace('/', '-')
    return f"LoanBooking_{hospital}_{date}.pdf"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/fill-form', methods=['POST'])
def fill_form_route():
    # Authenticate
    received_key = request.headers.get('X-API-Key', '<not provided>')
    if received_key != API_KEY:
        log.warning(f"Rejected request: received key='{received_key}' | expected key='{API_KEY}'")
        abort(401)

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({'error': 'Request body must be valid JSON'}), 400

    # Validate required fields
    required = ['hospital', 'surgery_date', 'surgeon_name', 'delivery_address']
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 422

    try:
        pdf_bytes = fill_pdf(payload)
    except Exception as e:
        log.exception("Error filling PDF")
        return jsonify({'error': str(e)}), 500

    filename = _make_filename(payload)
    flagged  = payload.get('flagged_kit', [])

    if flagged:
        log.warning(f"Flagged kit items not added to form: {flagged}")

    return jsonify({
        'status':       'success',
        'filename':     filename,
        'pdf_base64':   base64.b64encode(pdf_bytes).decode('utf-8'),
        'flagged_kit':  flagged,
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'template': PDF_TEMPLATE})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Pre-build the index so the first request isn't slow
    _build_product_index(PDF_TEMPLATE)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
