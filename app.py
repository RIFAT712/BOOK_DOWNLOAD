import os
import re
import json
import asyncio
import aiohttp
import img2pdf
import threading
import time
import uuid
import io  # <-- Import the 'io' module
from PIL import Image
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# --- No more BASE_FOLDER or file system operations for jobs ---

# --- Global State Management ---
# Manages all concurrent jobs. Key: job_id, Value: dictionary of job state.
JOBS = {}
JOBS_LOCK = threading.Lock() # To safely access the JOBS dictionary

# --- Async functions ---
async def fetch_json(session, url):
    """Fetches and parses JSON from a URL."""
    async with session.get(url) as resp:
        text = await resp.text(encoding="utf-8")
        # Handles the 'var xxx = {...};' format from fliphtml5
        return json.loads(text.split('= ')[1][:-1])

async def find_page_url(session, book_name, page_info):
    """Tries different URL patterns to find the correct image URL for a page."""
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
    """Downloads a single page into an in-memory bytes buffer."""
    pause_event = JOBS.get(job_id, {}).get("pause_event")
    if not pause_event:
        return None

    url = await find_page_url(session, book_name, page_info)
    if not url:
        print(f"URL not found for page {page_info['p'] + 1}")
        return None

    # --- MODIFICATION: Download image into memory ---
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
                # Return the page index and the raw bytes of the image
                return (page_info['p'], image_bytes_buffer.getvalue())
            else:
                return None
    except aiohttp.ClientError as e:
        print(f"Download failed for {url}: {e}")
        return None

async def process_book(book_name, job_id):
    """Main async task to process and download an entire book in memory."""
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
                    if job_id in JOBS: JOBS[job_id]["status"] = "failed"
                return

            fliphtml5_pages = config_dict['fliphtml5_pages']
            for i, page_data in enumerate(fliphtml5_pages):
                page_data['p'] = i

            with JOBS_LOCK:
                if job_id in JOBS: 
                    JOBS[job_id]["total"] = len(fliphtml5_pages)
                    # Store book title for the final PDF filename
                    JOBS[job_id]["book_title"] = re.sub(r'[<>:"/\\|?*]', '_', config_dict['meta']['title'])

            # --- MODIFICATION: Task calls download_page without folder_name ---
            tasks = [download_page(session, book_name, page_data, job_id) for page_data in fliphtml5_pages]
            downloaded_pages = await asyncio.gather(*tasks)
            # Sort pages by index (the first element of our tuple)
            downloaded_pages = sorted([p for p in downloaded_pages if p], key=lambda x: x[0])

            with JOBS_LOCK:
                if job_id not in JOBS: return
                if not downloaded_pages:
                    JOBS[job_id]["status"] = "failed"
                    return
                JOBS[job_id]["status"] = "preparing_pdf"

            # --- MODIFICATION: Compress images and generate PDF in memory ---
            compressed_image_bytes_list = []
            for _, image_data in downloaded_pages: # Loop through sorted (index, bytes) tuples
                img = Image.open(io.BytesIO(image_data))
                img = img.resize((int(img.width * 0.6), int(img.height * 0.6)), Image.Resampling.LANCZOS)
                
                compressed_buffer = io.BytesIO()
                img.save(compressed_buffer, "JPEG", quality=30)
                compressed_image_bytes_list.append(compressed_buffer.getvalue())

            # Convert the list of image bytes directly to a PDF in memory
            pdf_bytes = img2pdf.convert(compressed_image_bytes_list)

            with JOBS_LOCK:
                if job_id in JOBS:
                    # Store the final PDF data directly in the job dictionary
                    JOBS[job_id]["pdf_data"] = pdf_bytes
                    JOBS[job_id]["status"] = "done"
    except Exception as e:
        print(f"An error occurred in process_book for job {job_id}: {e}")
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "failed"

# --- Flask routes ---
# --- MODIFICATION: safe_delete_folder is no longer needed ---

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    book_name = request.form.get("book_name")
    if not book_name:
        return jsonify({"status": "error", "message": "Book ID required"}), 400

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
            "pdf_data": None, # --- MODIFICATION: Changed from pdf_file to pdf_data
            "book_title": "book", # Default title
            "paused": False,
            "started": True,
            "start_time": time.time(),
            "pause_event": pause_event,
            # --- MODIFICATION: No folder_name needed ---
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
        
        # Create a copy, excluding non-serializable objects for the JSON response
        state_to_send = {k: v for k, v in job_state.items() if k not in ['pause_event', 'pdf_data']}
    return jsonify(state_to_send)

@app.route("/download")
def download():
    """Downloads the final PDF from memory."""
    job_id = request.args.get("job_id")
    if not job_id:
        return "Job ID required", 400
        
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        pdf_data = job.get("pdf_data")
        book_title = job.get("book_title", "download")

    if pdf_data:
        # --- MODIFICATION: Serve the PDF from the in-memory bytes ---
        return send_file(
            io.BytesIO(pdf_data),
            as_attachment=True,
            download_name=f'{book_title}.pdf', # Set the filename for the user
            mimetype='application/pdf'
        )
    return "PDF not available or job not found.", 404

@app.route("/cancel", methods=["POST"])
def cancel():
    """Cancels a job. No file cleanup needed anymore."""
    job_id = request.form.get("job_id")
    if not job_id:
        return jsonify({"status": "error", "message": "Job ID required"}), 400

    with JOBS_LOCK:
        # Just remove the job from the dictionary. Python's garbage collector
        # will handle the memory (image data, PDF data, etc.).
        job_to_cancel = JOBS.pop(job_id, None)

    if job_to_cancel:
        return jsonify({"status": "cancelled", "job_id": job_id})
    else:
        return jsonify({"status": "error", "message": "Job not found"}), 404

if __name__ == "__main__":
    app.run(debug=True)