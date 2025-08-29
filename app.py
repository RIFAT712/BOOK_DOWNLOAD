import re
import json
import asyncio
import aiohttp
import img2pdf
import threading
import time
import uuid
import io
from PIL import Image
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# --- Global State Management ---
JOBS = {}
JOBS_LOCK = threading.Lock()

# --- Async functions ---
async def fetch_json(session, url):
    async with session.get(url) as resp:
        text = await resp.text(encoding="utf-8")
        return json.loads(text.split('= ')[1][:-1])

async def find_page_url(session, book_name, page_info):
    page_num_str = page_info["n"][0]
    page_index = page_info["p"] + 1
    
    url_options = [
        f'https://online.fliphtml5.com/{book_name}/files/large/{page_num_str}',
        f'https://online.fliphtml5.com/{book_name}/files/page/{page_index}.jpg',
        f'https://online.fliphtml5.com/{book_name}{page_num_str[1:]}'
    ]
    
    for url in url_options:
        try:
            async with session.head(url) as r:
                if r.status == 200:
                    return url
        except aiohttp.ClientError:
            continue
    return None

async def download_page(session, book_name, page_info, job_id):
    pause_event = JOBS.get(job_id, {}).get("pause_event")
    if not pause_event:
        return None

    url = await find_page_url(session, book_name, page_info)
    if not url:
        print(f"URL not found for page {page_info['p'] + 1}")
        return None

    image_bytes_buffer = io.BytesIO()
    try:
        async with session.get(url) as r:
            if r.status == 200:
                while True:
                    await pause_event.wait()
                    chunk = await r.content.read(8192)
                    if not chunk:
                        break
                    image_bytes_buffer.write(chunk)
                
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["done"] += 1
                return (page_info['p'], image_bytes_buffer.getvalue())
            else:
                return None
    except aiohttp.ClientError as e:
        print(f"Download failed for {url}: {e}")
        return None

async def process_book(book_name, job_id):
    try:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "downloading"

        config_file_url = f'https://online.fliphtml5.com/{book_name}/javascript/config.js'
        async with aiohttp.ClientSession() as session:
            try:
                config_dict = await fetch_json(session, config_file_url)
            except Exception as e:
                print(f"Failed to fetch or parse config.js: {e}")
                with JOBS_LOCK:
                    if job_id in JOBS:
                        JOBS[job_id]["status"] = "failed"
                return

            fliphtml5_pages = config_dict['fliphtml5_pages']
            for i, page_data in enumerate(fliphtml5_pages):
                page_data['p'] = i

            with JOBS_LOCK:
                if job_id in JOBS: 
                    JOBS[job_id]["total"] = len(fliphtml5_pages)
                    JOBS[job_id]["book_title"] = re.sub(
                        r'[<>:"/\\|?*]', '_', config_dict['meta']['title']
                    )

            tasks = [download_page(session, book_name, page_data, job_id) for page_data in fliphtml5_pages]
            downloaded_pages = await asyncio.gather(*tasks)
            downloaded_pages = sorted([p for p in downloaded_pages if p], key=lambda x: x[0])

            with JOBS_LOCK:
                if job_id not in JOBS:
                    return
                if not downloaded_pages:
                    JOBS[job_id]["status"] = "failed"
                    return
                JOBS[job_id]["status"] = "preparing_pdf"

            compressed_image_bytes_list = []
            for _, image_data in downloaded_pages:
                img = Image.open(io.BytesIO(image_data))
                img = img.resize((int(img.width * 0.6), int(img.height * 0.6)), Image.Resampling.LANCZOS)
                compressed_buffer = io.BytesIO()
                img.save(compressed_buffer, "JPEG", quality=15)
                compressed_image_bytes_list.append(compressed_buffer.getvalue())

            pdf_bytes = img2pdf.convert(compressed_image_bytes_list)

            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["pdf_data"] = pdf_bytes
                    JOBS[job_id]["status"] = "done"
    except Exception as e:
        print(f"An error occurred in process_book for job {job_id}: {e}")
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "failed"

# --- Flask routes ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    book_name = request.form.get("book_name")
    if not book_name or not re.match(r"^[A-Za-z0-9]+$", book_name):
        return jsonify({"status": "error", "message": "Invalid Book ID"}), 400

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        pause_event = asyncio.Event()
        pause_event.set()
        JOBS[job_id] = {
            "book_name": book_name,
            "job_id": job_id,
            "status": "starting",
            "total": 0,
            "done": 0,
            "pdf_data": None,
            "book_title": "book",
            "paused": False,
            "started": True,
            "start_time": time.time(),
            "pause_event": pause_event,
        }

    threading.Thread(target=lambda: asyncio.run(process_book(book_name, job_id))).start()
    return jsonify({"status": "started", "job_id": job_id})

@app.route("/pause", methods=["POST"])
def pause():
    job_id = request.form.get("job_id")
    action = request.form.get("action")
    if not job_id or job_id not in JOBS:
        return jsonify({"status": "error", "message": "Invalid job ID"}), 404

    with JOBS_LOCK:
        job = JOBS[job_id]
        if action == "pause":
            job["paused"] = True
            job["pause_event"].clear()
            status = "paused"
        elif action == "resume":
            job["paused"] = False
            job["pause_event"].set()
            status = "downloading"
        else:
            return jsonify({"status": "error", "message": "Invalid action"}), 400
    return jsonify({"status": status})

@app.route("/progress")
def get_progress():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"status": "error", "message": "Job ID required"}), 400

    with JOBS_LOCK:
        job_state = JOBS.get(job_id)
        if not job_state:
            return jsonify({"status": "not_found"}), 404
        state_to_send = {k: v for k, v in job_state.items() if k not in ['pause_event', 'pdf_data']}
    return jsonify(state_to_send)

@app.route("/download")
def download():
    job_id = request.args.get("job_id")
    if not job_id:
        return "Job ID required", 400
        
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        pdf_data = job.get("pdf_data")
        book_title = job.get("book_title", "download")

    if pdf_data:
        return send_file(
            io.BytesIO(pdf_data),
            as_attachment=True,
            download_name=f'{book_title}.pdf',
            mimetype='application/pdf'
        )
    return "PDF not available or job not found.", 404

@app.route("/cancel", methods=["POST"])
def cancel():
    job_id = request.form.get("job_id")
    if not job_id:
        return jsonify({"status": "error", "message": "Job ID required"}), 400

    with JOBS_LOCK:
        job_to_cancel = JOBS.pop(job_id, None)

    if job_to_cancel:
        return jsonify({"status": "cancelled", "job_id": job_id})
    else:
        return jsonify({"status": "error", "message": "Job not found"}), 404

# --- Entry point ---
if __name__ == "__main__":
    # Production-safe: disable debug
    app.run(host="0.0.0.0", port=8000, debug=False)
