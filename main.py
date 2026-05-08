# main.py — ORP Engine · PDF Stamp & Anchor Service (IMPROVED v2)
# Part of the OpenResPublica TruthChain stack.
# Must be launched via run_orp.sh or run_orp-gum.sh — never directly.

# ── IMPORTS ──────────────────────────────────────────────────────
# Standard library — no installation needed, comes with Python.
import hashlib      # SHA-256 fingerprinting
import io           # in-memory byte streams (PDF processing)
import os           # environment variables, file paths, signals
import json         # reading and writing JSON records
import datetime     # timestamps for records
import threading    # Lock() for control numbers, Timer for shutdown
import signal       # graceful shutdown on SIGINT / SIGTERM
import time         # retry delays
import fcntl        # file locking for process safety
import subprocess   # git commands
import logging      # structured logging

# Third-party — all listed in requirements.txt.
import gnupg        # GPG signing of audit records
import pytz         # Philippine timezone (Asia/Manila)
import qrcode       # QR code generation

from flask import (
    Flask,
    request,        # unpacks everything the browser sends (files, form data)
    send_file,      # returns binary files (stamped PDF) to the browser
    jsonify,        # returns JSON responses for errors
    render_template # renders portal.html through Jinja2
)
from pypdf import PdfReader, PdfWriter          # read and write PDF pages
from reportlab.pdfgen import canvas             # draw the stamp footer overlay
from reportlab.lib.pagesizes import A4          # standard page dimensions
from reportlab.lib.units import mm              # millimeter unit conversion
from reportlab.lib.utils import ImageReader     # embed QR image into PDF
from immudb.client import ImmudbClient          # immutable database client
from dotenv import load_dotenv                  # loads .env into os.environ
import getpass                                  # secure password prompt (no echo)

# Load .env file into environment before reading any variables.
load_dotenv()

# ── LOGGING CONFIGURATION ────────────────────────────────────────
# Structured logging with timestamps and severity levels
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ── 1. SECURITY ENVIRONMENT VALIDATION ───────────────────────────
# These three variables prove the engine was launched correctly
# via the shell boot sequence (_orp_core.sh).
# If any are missing, the engine halts immediately — no partial starts.

GPG_HOME      = os.getenv("GNUPGHOME")
GPG_EMAIL     = os.getenv("OPERATOR_GPG_EMAIL")
SSH_AUTH_SOCK = os.getenv("SSH_AUTH_SOCK")  # proves the GPG agent is live

if not all([GPG_HOME, GPG_EMAIL, SSH_AUTH_SOCK]):
    logger.critical("SECURITY FAILURE: Required environment variables missing")
    logger.critical(f"  - GPG_HOME: {'✅' if GPG_HOME else '❌ MISSING'}")
    logger.critical(f"  - GPG_EMAIL: {'✅' if GPG_EMAIL else '❌ MISSING'}")
    logger.critical(f"  - SSH_SOCK: {'✅' if SSH_AUTH_SOCK else '❌ MISSING'}")
    raise RuntimeError("Engine must be launched via run_orp.sh or run_orp-gum.sh")

# GPG_HOME must live in RAM (/dev/shm) — never on disk.
# If it's on disk, private keys survive a session, which is a vulnerability.
if not GPG_HOME.startswith("/dev/shm/"):
    logger.critical("VULNERABILITY: GNUPGHOME must be in RAM (/dev/shm)")
    raise RuntimeError("Launch via the boot script with RAM-based keyring")

# Initialize the GPG interface pointing at the ephemeral RAM keyring.
gpg = gnupg.GPG(gnupghome=GPG_HOME)
gpg.decode_errors = 'replace'
logger.info(f"✅ GPG environment initialized in {GPG_HOME}")


# ── 2. CONFIGURATION ─────────────────────────────────────────────
# All values come from .env — nothing is hardcoded.
# Defaults are safe fallbacks for development only.

IMMUDB_HOST = os.getenv("IMMUDB_HOST", "127.0.0.1:3322")
IMMUDB_USER = os.getenv("IMMUDB_USER", "immudb")
IMMUDB_DB   = os.getenv("IMMUDB_DB",   "defaultdb")

LGU_NAME    = os.getenv("LGU_NAME",             "Local Government Unit")
SIGNER_NAME = os.getenv("LGU_SIGNER_NAME",       "Authorized Signatory")
SIGNER_POS  = os.getenv("LGU_SIGNER_POSITION",   "Official")
TZ_NAME     = os.getenv("LGU_TIMEZONE",           "Asia/Manila")

REPO_PATH     = os.getenv("GITHUB_REPO_PATH",  "/home/orp/openrespublica.github.io")
GITHUB_PORTAL = os.getenv("GITHUB_PORTAL_URL", "https://openrespublica.github.io/verify.html")

# Control number file — persists the last issued number across sessions.
RECORDS_DIR  = os.path.join(REPO_PATH, "docs", "records")
CONTROL_FILE = os.path.join(REPO_PATH, "docs", "control_number.txt")

# Vault retry configuration
VAULT_MAX_RETRIES = int(os.getenv("VAULT_MAX_RETRIES", "3"))
VAULT_RETRY_DELAY = int(os.getenv("VAULT_RETRY_DELAY", "1"))  # seconds

# File upload constraints
MAX_PDF_SIZE = int(os.getenv("MAX_PDF_SIZE", "20 * 1024 * 1024"))  # 20MB


# ── 3. FLASK INITIALIZATION ───────────────────────────────────────
# static_folder='static' tells Flask to serve CSS, JS, and images
# automatically from the /static directory — no @app.route needed.
app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

# Threading primitives.
# ctrl_lock — ensures only one thread can issue a control number at a time.
# git_lock  — ensures only one thread pushes to GitHub at a time.
ctrl_lock = threading.Lock()
git_lock  = threading.Lock()

# Ensure the records directory exists before anything tries to write to it.
os.makedirs(RECORDS_DIR, exist_ok=True)
logger.info(f"✅ Records directory ready: {RECORDS_DIR}")


# ── 4. VAULT CONNECTION ───────────────────────────────────────────
# Cached vault password — prompted exactly once at startup.
# Reconnects (on session timeout) reuse this value silently,
# so the web server never blocks waiting for terminal input.
_vault_password: str | None = None

def get_client() -> ImmudbClient:
    """
    Connect to the immudb vault with explicit host:port parsing.
    Prompts for password on first call only — subsequent calls reuse
    the cached value so reconnects never block the web server.
    """
    global _vault_password

    # Split host:port explicitly — avoids gRPC defaulting to port 443.
    if ":" in IMMUDB_HOST:
        host, port = IMMUDB_HOST.rsplit(":", 1)
        try:
            port = int(port)
        except ValueError:
            logger.error(f"Invalid port in IMMUDB_HOST: {IMMUDB_HOST}")
            port = 3322
    else:
        host, port = IMMUDB_HOST, 3322

    if _vault_password is None:
        logger.info("Prompting for vault password...")
        _vault_password = getpass.getpass(
            f"Enter password for vault user [{IMMUDB_USER}]: "
        )

    try:
        c = ImmudbClient(f"{host}:{port}")
        c.login(IMMUDB_USER, _vault_password, database=IMMUDB_DB)
        logger.info(f"✅ Vault unlocked → {host}:{port}/{IMMUDB_DB}")
        return c
    except Exception as e:
        logger.error(f"Vault access denied: {e}")
        raise

# Connect once at startup — Flask routes reuse this global client.
try:
    client = get_client()
except Exception as e:
    logger.critical(f"Failed to initialize vault connection: {e}")
    exit(1)


# ── 5. GRACEFUL SHUTDOWN ──────────────────────────────────────────
def graceful_shutdown(signum, frame):
    """
    Called when SIGINT or SIGTERM is received.
    Logs out of immudb, then forces exit so the shell trap in
    run_orp.sh / run_orp-gum.sh fires the RAM disk cleanup.
    """
    logger.warning("Received shutdown signal — purging session...")
    try:
        client.logout()
        logger.info("✅ Vault session closed")
    except Exception as e:
        logger.warning(f"Vault logout error: {e}")
    os._exit(0)  # os._exit bypasses Python cleanup to ensure shell trap fires

signal.signal(signal.SIGINT,  graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ── 6. CRYPTO & DATA UTILITIES ───────────────────────────────────

def sign_json_data(record: dict) -> dict | None:
    """
    Signs the audit record JSON using the ephemeral GPG key in RAM.
    Returns the signature block to embed in the record, or None on failure.
    The key never touches disk — it lives only in /dev/shm for this session.
    """
    try:
        data_str = json.dumps(record, sort_keys=True)
        sig      = gpg.sign(data_str, keyid=GPG_EMAIL)

        if sig.status != "signature created":
            logger.error(f"GPG signing failed: {sig.stderr}")
            return None

        return {
            "gpg_signature":   str(sig),
            "hash_anchor":     hashlib.sha256(data_str.encode()).hexdigest(),
            "integrity_scope": "EPHEMERAL_RAM_LEGAL_SIGNATURE",
        }
    except Exception as e:
        logger.error(f"Error during JSON signing: {e}")
        return None


def next_control_number() -> str:
    """
    Issues the next sequential control number for this calendar year.
    
    Uses both threading.Lock AND file locking for process safety.
    The threading lock prevents race conditions between threads.
    The fcntl file lock prevents race conditions between processes.
    
    Format: YYYY-NNNN  (e.g. 2026-0042)
    """
    with ctrl_lock:
        local_tz     = pytz.timezone(TZ_NAME)
        current_year = str(datetime.datetime.now(local_tz).year)

        # Create the control file atomically if this is the first issuance ever.
        if not os.path.exists(CONTROL_FILE):
            try:
                fd = os.open(CONTROL_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, b"2026-0000")
                os.close(fd)
            except FileExistsError:
                # Another process created it between our check and creation
                pass

        # Use file locking for process-level safety
        with open(CONTROL_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock
            try:
                content = f.read().strip()
                if not content:
                    parts = [current_year, 0]
                else:
                    parts = content.split("-")
                    
                year, num = parts[0], int(parts[1]) if len(parts) > 1 else 0

                # Reset counter on new year.
                if year != current_year:
                    year, num = current_year, 0

                new_ctrl = f"{year}-{(num + 1):04d}"
                f.seek(0)
                f.write(new_ctrl)
                f.truncate()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)  # Release lock

        logger.info(f"Control number issued: {new_ctrl}")
        return new_ctrl


def generate_qr(sha256_hash: str) -> tuple[io.BytesIO, str]:
    """
    Generates a QR code that encodes the public verification URL
    for this document. Scanning the QR opens the public ledger
    pre-filtered to this exact document's hash.
    """
    try:
        qr_url = f"{GITHUB_PORTAL}?hash={sha256_hash}"
        qr     = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_url)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        buf.seek(0)
        return buf, qr_url
    except Exception as e:
        logger.error(f"QR code generation failed: {e}")
        raise


def add_footer(
    original_pdf:   bytes,
    sha256_hash:    str,
    qr_buf:         io.BytesIO,
    timestamp:      str,
    control_number: str,
) -> io.BytesIO:
    """
    Stamps every page of the PDF with:
      - A horizontal rule separating content from the stamp
      - Timestamp, control number, and truncated hash
      - QR code linking to the public verification portal

    Uses ReportLab to draw on an overlay canvas, then merges
    that overlay onto each page using pypdf.
    """
    try:
        reader   = PdfReader(io.BytesIO(original_pdf))
        writer   = PdfWriter()
        qr_image = ImageReader(qr_buf)

        for page in reader.pages:
            packet = io.BytesIO()
            c      = canvas.Canvas(packet, pagesize=A4)

            # Horizontal rule above the stamp area.
            c.setLineWidth(0.5)
            c.line(25 * mm, 22 * mm, 185 * mm, 22 * mm)

            # Three metadata fields beneath the rule.
            items = [
                ("TIMESTAMP", timestamp),
                ("CTRL NO",   control_number),
                ("HASH",      sha256_hash[:32] + "..."),
            ]
            y = 18 * mm
            for label, val in items:
                c.setFont("Helvetica-Bold", 7)
                c.drawString(30 * mm, y, f"{label}:")
                c.setFont("Helvetica", 7)
                c.drawString(55 * mm, y, str(val))
                y -= 3.5 * mm

            # QR code in the bottom-right corner of the stamp area.
            c.drawImage(qr_image, 165 * mm, 5 * mm, width=15 * mm, height=15 * mm)
            c.save()
            packet.seek(0)

            # Merge the stamp overlay onto the original page.
            page.merge_page(PdfReader(packet).pages[0])
            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        out.seek(0)
        return out
    except Exception as e:
        logger.error(f"PDF stamping failed: {e}")
        raise


def update_manifest(record: dict) -> None:
    """
    Prepends the new record to manifest.json (newest first).
    Capped at 1,000 entries to prevent unbounded growth.
    The manifest is the data source for the public ledger page.
    """
    try:
        manifest_path = os.path.join(RECORDS_DIR, "manifest.json")
        records: list = []

        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r") as f:
                    records = json.load(f)
            except Exception as e:
                logger.warning(f"Could not read manifest (will overwrite): {e}")
                records = []

        records.insert(0, record)
        records = records[:1000]

        with open(manifest_path, "w") as f:
            json.dump(records, f, indent=2)
        logger.info(f"✅ Manifest updated: {len(records)} records")
    except Exception as e:
        logger.error(f"Manifest update failed: {e}")
        raise


def sync_to_github(json_path: str, record: dict) -> None:
    """
    Runs in a background daemon thread.
    Writes the manifest, commits the new record JSON, and pushes
    to GitHub Pages so the public ledger updates within ~60 seconds.

    The git_lock ensures only one push happens at a time even if
    two documents are processed in rapid succession.
    """
    with git_lock:
        try:
            update_manifest(record)

            anchor_hash = os.path.basename(json_path).replace(".json", "")
            git_env = os.environ.copy()
            git_env["SSH_AUTH_SOCK"]   = SSH_AUTH_SOCK
            git_env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no"

            subprocess.run(
                ['git', '-C', REPO_PATH, 'add', '.'],
                check=True, env=git_env, capture_output=True
            )
            subprocess.run(
                ['git', '-C', REPO_PATH, 'commit', '-m',
                 f"Audit: Anchor {anchor_hash}"],
                check=False, env=git_env, capture_output=True
            )
            subprocess.run(
                ['git', '-C', REPO_PATH, 'fetch', 'origin'],
                check=True, env=git_env, capture_output=True
            )
            subprocess.run(
                ['git', '-C', REPO_PATH, 'pull', '--rebase',
                 '-X', 'ours', 'origin', 'main'],
                check=True, env=git_env, capture_output=True
            )
            subprocess.run(
                ['git', '-C', REPO_PATH, 'push', 'origin', 'main'],
                check=True, env=git_env, capture_output=True
            )
            logger.info(f"✅ TruthChain synchronized: {anchor_hash}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Git sync error: {e}")
        except Exception as e:
            logger.error(f"Sync thread error: {e}")


def start_sync(json_path: str, record: dict) -> None:
    """
    Launches sync_to_github in a daemon thread.
    daemon=True means the thread will not block process exit —
    the cleanup trap in the shell script fires cleanly.
    """
    threading.Thread(
        target=sync_to_github,
        args=(json_path, record),
        daemon=True
    ).start()


# ── 7. ROUTES ──────────��─────────────────────────────────────────

@app.route("/")
def home():
    """
    Serves the operator portal.
    Jinja2 renders portal.html, resolving {{ url_for(...) }} into
    real static file paths before sending HTML to the browser.
    CSS and JS are then fetched automatically via Flask's built-in
    static file handler — no additional routes needed.
    """
    return render_template("portal.html")


@app.route("/cert_error.html")
def cert_error():
    """
    Fallback route for the Nginx error_page 495/496 directive.
    In practice the named @cert_error location in orp_engine.conf
    handles this inline — this route exists as a safety net only.
    """
    return (
        "<h1>Sovereign Identity Required</h1>"
        "<p>A valid operator certificate is required.</p>",
        403,
    )


@app.route('/lock_engine', methods=['POST'])
def lock_engine():
    """
    Secure kill switch — triggered by the Lock Engine button in the portal.
    Fires SIGINT after a 0.5s delay so Flask can send the 200 response
    before the process exits. Without the delay, the browser gets a
    connection error instead of a clean confirmation.
    SIGINT → graceful_shutdown() → shell trap → RAM disk wiped.
    """
    logger.warning("Lock signal received — initiating secure shutdown")
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    return "Engine locked. RAM disk purged.", 200


@app.route("/upload", methods=["POST"])
def upload_pdf():
    """
    Core route — the entire purpose of ORP Engine.

    Pipeline:
      1. Validate the uploaded file (PDF only, size limit)
      2. Compute SHA-256 fingerprint
      3. Anchor hash to immudb (immutable, append-only) with retry logic
      4. Issue a unique control number (thread-safe + process-safe)
      5. GPG-sign the audit record
      6. Save the JSON record locally
      7. Sync to GitHub Pages (background daemon thread)
      8. Stamp the PDF with QR + metadata footer
      9. Return the stamped PDF to the browser for download
    """
    global client

    try:
        # ── Step 1: Validate ─────────────────────────────────────────
        file = request.files.get("document")
        if not file or not file.filename.lower().endswith('.pdf'):
            logger.warning(f"Invalid file type: {file.filename if file else 'None'}")
            return "Only PDF files are accepted.", 400

        doc_type  = request.form.get("doc_type", "BARANGAY-CERT")
        pdf_bytes = file.read()

        if len(pdf_bytes) > MAX_PDF_SIZE:
            logger.warning(f"File too large: {len(pdf_bytes)} bytes")
            return "File exceeds maximum size (20MB).", 413

        # ── Step 2: Fingerprint ──────────────────────────────────────
        sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()
        logger.info(f"Processing document: {sha256_hash[:16]}...")

        # ── Step 3: Anchor to immudb with RETRY LOGIC ─────────────────
        tx = None
        last_error = None
        
        for attempt in range(1, VAULT_MAX_RETRIES + 1):
            try:
                logger.info(f"Anchoring hash (attempt {attempt}/{VAULT_MAX_RETRIES})...")
                tx = client.set(sha256_hash.encode(), b"VERIFIED_BY_ORP_ENGINE")
                logger.info(f"✅ Hash anchored with tx_id={tx.id}")
                break
            except Exception as e:
                last_error = e
                if attempt < VAULT_MAX_RETRIES:
                    logger.warning(f"Vault error (attempt {attempt}): {e}")
                    logger.info(f"Retrying in {VAULT_RETRY_DELAY}s...")
                    time.sleep(VAULT_RETRY_DELAY)
                    
                    try:
                        logger.info("Reconnecting to vault...")
                        client = get_client()
                    except Exception as reconnect_error:
                        logger.warning(f"Reconnection failed: {reconnect_error}")
                        last_error = reconnect_error
                else:
                    logger.error(f"Vault unavailable after {VAULT_MAX_RETRIES} attempts")

        if tx is None:
            logger.error(f"Failed to anchor: {last_error}")
            return jsonify({
                "status": "ERROR",
                "message": f"Failed to anchor hash after {VAULT_MAX_RETRIES} attempts",
                "error": str(last_error),
                "sha256": sha256_hash
            }), 503

        # ── Step 4: Control number & timestamp ───────────────────────
        local_tz     = pytz.timezone(TZ_NAME)
        timestamp_ph = datetime.datetime.now(local_tz).strftime("%Y-%m-%d %I:%M %p PHT")
        control_no   = next_control_number()
        final_ctrl   = f"Verified_{control_no}-{doc_type}"

        # ── Step 5: Assemble & sign the audit record ─────────────────
        operator_identity = request.headers.get('X-Operator-ID', 'UNKNOWN')

        record = {
            "status":                "VERIFIED ✅",
            "signer":                SIGNER_NAME,
            "position":              f"{SIGNER_POS}, {LGU_NAME}",
            "operator_identity":     operator_identity,
            "document_type":         doc_type,
            "control_number":        final_ctrl,
            "sha256_hash":           sha256_hash,
            "timestamp":             timestamp_ph,
            "immudb_transaction_id": tx.id,
            "verification_url":      f"{GITHUB_PORTAL}?hash={sha256_hash}",
        }

        pgp_sig = sign_json_data(record)
        if pgp_sig:
            record["data_signature"] = pgp_sig

        # ── Step 6: Save JSON record locally ─────────────────────────
        json_path = os.path.join(RECORDS_DIR, f"{sha256_hash}.json")
        with open(json_path, "w") as f:
            json.dump(record, f, indent=2)

        # ── Step 7: Sync to GitHub (background) ──────────────────────
        start_sync(json_path, record)

        # ── Step 8: Stamp the PDF ────────────────────────────────────
        qr_buf, _       = generate_qr(sha256_hash)
        stamped_pdf_buf = add_footer(
            pdf_bytes, sha256_hash, qr_buf, timestamp_ph, final_ctrl
        )

        # ── Step 9: Return stamped PDF to browser ────────────────────
        logger.info(f"✅ Upload complete: {final_ctrl}")
        return send_file(
            stamped_pdf_buf,
            as_attachment=True,
            download_name=f"{final_ctrl}.pdf"
        )

    except Exception as e:
        logger.error(f"Unhandled error in upload: {e}", exc_info=True)
        return jsonify({
            "status": "ERROR",
            "message": "An unexpected error occurred",
            "error": str(e)
        }), 500


# ── 8. ENTRY POINT ───────────────────────────────────────────────
if __name__ == "__main__":
    # Binds to 127.0.0.1 only — Nginx is the public-facing gateway.
    # Flask is never exposed directly to the network.
    port = int(os.getenv("FLASK_PORT", 5000))
    logger.info(f"Starting ORP Engine on 127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
