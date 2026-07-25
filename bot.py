import os
import zipfile
import shutil
import tempfile
import threading
import re
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from github import Github, GithubException
from flask import Flask

# ---------- Environment ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USERNAME")
PORT = int(os.getenv("PORT", 8080))

# ---------- Flask for health check ----------
app_flask = Flask(__name__)
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=PORT)

# ---------- Temp dir ----------
TEMP_DIR = tempfile.mkdtemp(prefix="bot_zip_")
WAITING_FOR_REPO_NAME = 1

# ---------- Helper: Upload folder to GitHub ----------
async def upload_folder_to_github(repo, folder_path, branch="main"):
    uploaded_count = 0
    errors = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_path = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, folder_path).replace("\\", "/")
            try:
                with open(file_path, 'rb') as f:
                    content = f.read()
                try:
                    repo.create_file(relative_path, f"Upload {relative_path}", content, branch=branch)
                except GithubException as e:
                    if e.status == 422:  # already exists
                        file_content = repo.get_contents(relative_path, ref=branch)
                        repo.update_file(relative_path, f"Update {relative_path}", content, file_content.sha, branch=branch)
                    else:
                        raise
                uploaded_count += 1
            except Exception as e:
                errors.append(f"{relative_path}: {str(e)}")
    if errors:
        return f"Uploaded {uploaded_count} files, but errors: " + ", ".join(errors[:3])
    return f"Upload Complete – {uploaded_count} files uploaded."

# ---------- Helper: Download GitHub repo as ZIP ----------
def download_github_repo_as_zip(repo_url, download_path):
    """Downloads a public GitHub repo as ZIP and saves to download_path"""
    # Extract username/repo from URL
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url)
    if not match:
        return None, "Invalid GitHub URL"
    username, repo = match.groups()
    zip_url = f"https://github.com/{username}/{repo}/archive/main.zip"
    
    # Try main branch, fallback to master
    response = requests.get(zip_url)
    if response.status_code != 200:
        zip_url = f"https://github.com/{username}/{repo}/archive/master.zip"
        response = requests.get(zip_url)
        if response.status_code != 200:
            return None, "Repo not accessible or empty"
    
    with open(download_path, 'wb') as f:
        f.write(response.content)
    return download_path, f"Downloaded {repo}"

# ---------- Conversation: ZIP upload ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 मुझे **ZIP फाइल** भेजो या **GitHub रिपो का URL** (जैसे https://github.com/user/repo)\n"
        "मैं तुमसे रिपॉजिटरी का नाम पूछूँगा, फिर GitHub पर अपलोड कर दूँगा।"
    )

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles both ZIP files and GitHub URLs"""
    user = update.message.from_user
    text = update.message.text
    document = update.message.document

    # ---- CASE 1: GitHub URL ----
    if text and ("github.com" in text.lower()):
        context.user_data['pending_github_url'] = text.strip()
        await update.message.reply_text("📝 इस रिपो के लिए नई रिपॉजिटरी का नाम बताओ:")
        return WAITING_FOR_REPO_NAME

    # ---- CASE 2: ZIP file ----
    elif document and document.file_name.endswith('.zip'):
        context.user_data['pending_zip'] = {
            'file_id': document.file_id,
            'file_name': document.file_name,
            'user_id': user.id,
        }
        await update.message.reply_text("📝 इस ZIP के लिए रिपॉजिटरी का नाम बताओ (बिना स्पेस):")
        return WAITING_FOR_REPO_NAME

    else:
        await update.message.reply_text("❌ सिर्फ ZIP फाइल या GitHub URL भेजो!")
        return ConversationHandler.END

async def handle_repo_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_name = update.message.text.strip()
    if not repo_name:
        await update.message.reply_text("❌ नाम खाली नहीं हो सकता। फिर से भेजो।")
        return WAITING_FOR_REPO_NAME

    await update.message.reply_text("⏳ प्रोसेस हो रहा है...")

    # ---- Check if it's a GitHub URL or ZIP ----
    github_url = context.user_data.get('pending_github_url')
    zip_pending = context.user_data.get('pending_zip')
    extract_path = None
    zip_path = None
    download_msg = ""

    try:
        # ---- SCENARIO A: GitHub URL ----
        if github_url:
            zip_path = os.path.join(TEMP_DIR, f"repo_{repo_name}.zip")
            status, msg = download_github_repo_as_zip(github_url, zip_path)
            if not status:
                await update.message.reply_text(f"❌ {msg}")
                context.user_data.pop('pending_github_url', None)
                return ConversationHandler.END
            download_msg = f"📥 {msg} – अब इसे अपलोड कर रहा हूँ..."
            await update.message.reply_text(download_msg)
            
            # Extract the downloaded ZIP
            extract_path = os.path.join(TEMP_DIR, "extracted_" + str(update.message.from_user.id))
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            # Send the ZIP back to user
            await update.message.reply_document(document=open(zip_path, 'rb'), caption="📦 ये रहा तुम्हारा डाउनलोड किया गया ZIP")

        # ---- SCENARIO B: ZIP file ----
        elif zip_pending:
            file_id = zip_pending['file_id']
            file = await context.bot.get_file(file_id)
            zip_path = os.path.join(TEMP_DIR, zip_pending['file_name'])
            await file.download_to_drive(zip_path)
            extract_path = os.path.join(TEMP_DIR, "extracted_" + str(zip_pending['user_id']))
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        else:
            await update.message.reply_text("❌ पहले ZIP या URL भेजो!")
            return ConversationHandler.END

        # ---- Clean top-level folder if needed ----
        items = os.listdir(extract_path)
        if len(items) == 1 and os.path.isdir(os.path.join(extract_path, items[0])):
            top_folder = items[0]
            top_path = os.path.join(extract_path, top_folder)
            for item in os.listdir(top_path):
                shutil.move(os.path.join(top_path, item), extract_path)
            os.rmdir(top_path)

        # ---- GitHub: Create repo and upload ----
        g = Github(GITHUB_TOKEN)
        user_obj = g.get_user()
        base_name = repo_name
        attempt = 0
        while True:
            try:
                user_obj.get_repo(base_name)
                attempt += 1
                base_name = f"{repo_name}-{attempt}"
            except GithubException as e:
                if e.status == 404:
                    break
                else:
                    raise

        await update.message.reply_text(f"🏗️ '{base_name}' बन रहा है...")
        repo = user_obj.create_repo(base_name, private=False, auto_init=False)
        upload_status = await upload_folder_to_github(repo, extract_path)
        await update.message.reply_text(f"✅ Repo: {repo.html_url}\n📤 {upload_status}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Cleanup
        if extract_path and os.path.exists(extract_path):
            shutil.rmtree(extract_path, ignore_errors=True)
        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
        context.user_data.pop('pending_zip', None)
        context.user_data.pop('pending_github_url', None)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ कैंसल कर दिया गया।")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Bot startup ----------
def run_bot():
    app = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, handle_input),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)
        ],
        states={
            WAITING_FOR_REPO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_repo_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    
    app.bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Bot चल रहा है...")
    app.run_polling(drop_pending_updates=True)

# ---------- Main ----------
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
