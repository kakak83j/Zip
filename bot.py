import os
import zipfile
import shutil
import tempfile
import threading
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from github import Github, GithubException
from flask import Flask, request, jsonify

# Environment Variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USERNAME")
PORT = int(os.getenv("PORT", 8080))

# Flask app for health checks
app_flask = Flask(__name__)

@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=PORT)

# Temporary directory for zip processing
TEMP_DIR = tempfile.mkdtemp(prefix="bot_zip_")

# ------------------------------------------------------------------
# GitHub Upload Function (recursive)
# ------------------------------------------------------------------
async def upload_folder_to_github(repo, folder_path, branch="main"):
    try:
        all_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, folder_path)
                all_files.append((relative_path, file_path))
        if not all_files:
            return "Repo empty (no files)"
        for rel_path, abs_path in all_files:
            with open(abs_path, 'rb') as f:
                content = f.read()
            try:
                repo.create_file(rel_path, f"Upload {rel_path}", content, branch=branch)
            except GithubException as e:
                if e.status == 422:  # file already exists
                    file_content = repo.get_contents(rel_path, ref=branch)
                    repo.update_file(rel_path, f"Update {rel_path}", content, file_content.sha, branch=branch)
        return "Upload Complete"
    except Exception as e:
        return f"Upload Error: {str(e)}"

# ------------------------------------------------------------------
# Telegram Handlers (async)
# ------------------------------------------------------------------
async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    document = update.message.document
    if not document.file_name.endswith('.zip'):
        await update.message.reply_text("❌ सिर्फ ZIP फाइल भेजो!")
        return
    await update.message.reply_text("📥 Zip डाउनलोड हो रहा है...")
    file = await context.bot.get_file(document.file_id)
    zip_path = os.path.join(TEMP_DIR, document.file_name)
    await file.download_to_drive(zip_path)
    await update.message.reply_text("📂 Unzip हो रहा है...")
    extract_path = os.path.join(TEMP_DIR, "extracted_" + str(user.id))
    os.makedirs(extract_path, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_path)
    repo_name = document.file_name.replace('.zip', '').replace(' ', '-')
    await update.message.reply_text(f"🏗️ GitHub पर '{repo_name}' बन रहा है...")
    g = Github(GITHUB_TOKEN)
    user_obj = g.get_user()
    try:
        repo = user_obj.create_repo(repo_name, private=False, auto_init=False)
        await update.message.reply_text(f"✅ Repo बन गया: {repo.html_url}")
        status = await upload_folder_to_github(repo, extract_path)
        await update.message.reply_text(f"📤 {status}\n🔗 {repo.html_url}")
    except GithubException as e:
        await update.message.reply_text(f"❌ GitHub Error: {e.data.get('message', str(e))}")
    except Exception as e:
        await update.message.reply_text(f"❌ Unexpected: {str(e)}")
    finally:
        shutil.rmtree(extract_path, ignore_errors=True)
        os.remove(zip_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Zip भेजो, मैं उसे GitHub Repo में बदल दूंगा।")

# ------------------------------------------------------------------
# Bot startup – synchronous (runs polling)
# ------------------------------------------------------------------
def run_bot():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_zip))
    print("🤖 Bot चल रहा है...")
    app.run_polling()   # This is synchronous and blocks

# ------------------------------------------------------------------
# Main – start Flask in a thread, then run bot in main thread
# ------------------------------------------------------------------
if __name__ == "__main__":
    # Start Flask in a daemon thread (so it doesn't block)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Run the bot (blocking) in the main thread
    run_bot()
