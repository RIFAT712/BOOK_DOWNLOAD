import os, re, json, asyncio, aiohttp, img2pdf, threading, time, uuid, io
from PIL import Image
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
JOBS = {}
JOBS_LOCK = threading.Lock()

async def fetch_json(session, url):
    async with session.get(url) as resp:
        text = await resp.text(encoding="utf-8")
        return json.loads(text.split('= ')[1][:-1])

async def find_page_url(session, book_name, page_info):
    page_num_str = page_info["n"][0]
    page_index = page_info["p"] + 1
    urls=[
        f'https://online.fliphtml5.com/{book_name}/files/large/{page_num_str}',
        f'https://online.fliphtml5.com/{book_name}/files/page/{page_index}.jpg',
        f'https://online.fliphtml5.com/{book_name}{page_num_str[1:]}'
    ]
    for url in urls:
        try:
            async with session.head(url) as r:
                if r.status==200: return url
        except: continue
    return None

async def download_page(session, book_name, page_info, job_id):
    pause_event=JOBS.get(job_id,{}).get("pause_event")
    if not pause_event: return None
    url=await find_page_url(session, book_name, page_info)
    if not url: return None
    buffer=io.BytesIO()
    try:
        async with session.get(url) as r:
            if r.status==200:
                while True:
                    await pause_event.wait()
                    chunk=await r.content.read(8192)
                    if not chunk: break
                    buffer.write(chunk)
                with JOBS_LOCK:
                    if job_id in JOBS: JOBS[job_id]["done"]+=1
                return (page_info['p'], buffer.getvalue())
            else: return None
    except: return None

async def process_book(book_name, job_id):
    try:
        with JOBS_LOCK: JOBS[job_id]["status"]="downloading"
        config_file_url=f'https://online.fliphtml5.com/{book_name}/javascript/config.js'
        async with aiohttp.ClientSession() as session:
            try: config_dict=await fetch_json(session, config_file_url)
            except: 
                with JOBS_LOCK:
                    if job_id in JOBS: JOBS[job_id]["status"]="failed"
                return

            pages=config_dict['fliphtml5_pages']
            for i,page in enumerate(pages): page['p']=i
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["total"]=len(pages)
                    JOBS[job_id]["book_title"]=re.sub(r'[<>:"/\\|?*]','_',config_dict['meta']['title'])
                    JOBS[job_id]["compression_total"]=len(pages)
                    JOBS[job_id]["compression_done"]=0

            tasks=[download_page(session, book_name,page,job_id) for page in pages]
            downloaded_pages=await asyncio.gather(*tasks)
            downloaded_pages=sorted([p for p in downloaded_pages if p], key=lambda x:x[0])

            with JOBS_LOCK:
                if job_id not in JOBS: return
                if not downloaded_pages: JOBS[job_id]["status"]="failed"; return
                JOBS[job_id]["status"]="compressing"

            compressed_images=[]
            for _,img_bytes in downloaded_pages:
                img=Image.open(io.BytesIO(img_bytes))
                img=img.resize((int(img.width*0.6),int(img.height*0.6)), Image.Resampling.LANCZOS)
                buf=io.BytesIO()
                img.save(buf,"JPEG",quality=15)
                compressed_images.append(buf.getvalue())
                with JOBS_LOCK: JOBS[job_id]["compression_done"]+=1

            with JOBS_LOCK: JOBS[job_id]["status"]="creating_pdf"
            pdf_bytes=img2pdf.convert(compressed_images)
            with JOBS_LOCK:
                JOBS[job_id]["pdf_data"]=pdf_bytes
                JOBS[job_id]["status"]="done"
    except:
        with JOBS_LOCK:
            if job_id in JOBS: JOBS[job_id]["status"]="failed"

def start_background_job(book_name, job_id):
    loop=asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(process_book(book_name, job_id))
    loop.close()

@app.route("/")
def index(): return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    book_name=request.form.get("book_name")
    if not book_name: return jsonify({"status":"error","message":"Book ID required"}),400
    job_id=str(uuid.uuid4())
    with JOBS_LOCK:
        pause_event=asyncio.Event(); pause_event.set()
        JOBS[job_id]={ "book_name":book_name, "job_id":job_id, "status":"starting",
            "total":0, "done":0, "compression_total":0, "compression_done":0,
            "pdf_data":None, "book_title":"book", "paused":False,
            "started":True, "start_time":time.time(), "pause_event":pause_event }
    threading.Thread(target=start_background_job,args=(book_name,job_id)).start()
    return jsonify({"status":"started","job_id":job_id})

@app.route("/pause", methods=["POST"])
def pause():
    job_id=request.form.get("job_id")
    action=request.form.get("action")
    if not job_id or job_id not in JOBS: return jsonify({"status":"error","message":"Invalid job ID"}),404
    with JOBS_LOCK:
        job=JOBS[job_id]
        if action=="pause": job["paused"]=True; job["pause_event"].clear(); status="paused"
        elif action=="resume": job["paused"]=False; job["pause_event"].set(); status="downloading"
        else: return jsonify({"status":"error","message":"Invalid action"}),400
    return jsonify({"status":status})

@app.route("/progress")
def get_progress():
    job_id=request.args.get("job_id")
    if not job_id: return jsonify({"status":"error","message":"Job ID required"}),400
    with JOBS_LOCK:
        job=JOBS.get(job_id)
        if not job: return jsonify({"status":"not_found"}),404
        state={k:v for k,v in job.items() if k not in ['pause_event','pdf_data']}
    return jsonify(state)

@app.route("/download")
def download():
    job_id=request.args.get("job_id")
    if not job_id: return "Job ID required",400
    with JOBS_LOCK:
        job=JOBS.get(job_id)
        if not job: return "Job not found",404
        pdf_data=job.get("pdf_data")
        book_title=job.get("book_title","download")
    if pdf_data:
        return send_file(io.BytesIO(pdf_data), as_attachment=True, download_name=f"{book_title}.pdf", mimetype="application/pdf")
    return "PDF not ready yet.",404

@app.route("/cancel", methods=["POST"])
def cancel():
    job_id=request.form.get("job_id")
    if not job_id: return jsonify({"status":"error","message":"Job ID required"}),400
    with JOBS_LOCK:
        job=JOBS.pop(job_id,None)
    if job: return jsonify({"status":"cancelled","job_id":job_id})
    return jsonify({"status":"error","message":"Job not found"}),404

if __name__=="__main__":
    port=int(os.environ.get("PORT",8000))
    app.run(host="0.0.0.0", port=port)
