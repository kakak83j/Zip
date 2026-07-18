import os
import zipfile
import shutil
import tempfile
import threading
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
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

# Conversation state
WAITING_FOR_REPO_NAME = 1

# ------------------------------------------------------------------
# GitHub Upload Function (robust – uploads all files/folders)
# ------------------------------------------------------------------
async def upload_folder_to_github(repo, folder_path, branch="main"):
    """Recursively uploads all files/folders from folder_path to repo root."""
    uploaded_count = 0
    errors = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_path = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, folder_path)
            # Ensure forward slashes for GitHub API (optional but safe)
            relative_path = relative_path.replace("\\", "/")
            try:
                with open(file_path, 'rb') as f:
                    content = f.read()
                try:
                    repo.create_file(relative_path, f"Upload {relative_path}", content, branch=branch)
                except GithubException as e:
                    if e.status == 422:  # file already exists
                        file_content = repo.get_contents(relative_path, ref=branch)
                        repo.update_file(relative_path, f"Update {relative_path}", content, file_content.sha, branch=branch)
                    else:
                        raise
                uploaded_count += 1
            except Exception as e:
                errors.append(f"{relative_path}: {str(e)}")
    if errors:
        return f"Uploaded {uploaded_count} files, but errors on: " + ", ".join(errors[:3])
    return f"Upload Complete – {uploaded_count} files uploaded."

# ------------------------------------------------------------------
# Telegram Handlers
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 मुझे एक ZIP फाइल भेजो।\n"
        "मैं तुमसे रिपॉजिटरी का नाम पूछूँगा, फिर GitHub पर अपलोड कर दूँगा।"
    )

async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives zip, asks for repo name, stores file info."""
    user = update.message.from_user
    document = update.message.document
    if not document.file_name.endswith('.zip'):
        await update.message.reply_text("❌ सिर्फ ZIP फाइल भेजो!")
        return

    # Save file info in context for later
    context.user_data['pending_zip'] = {
        'file_id': document.file_id,
        'file_name': document.file_name,
        'user_id': user.id,
    }
    await update.message.reply_text("📝 कृपया इस रिपॉजिटरी का नाम टाइप करो (अंग्रेज़ी में, बिना स्पेस):")
    return WAITING_FOR_REPO_NAME

async def handle_repo_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives repo name, processes the zip."""
    repo_name = update.message.text.strip()
    if not repo_name:
        await update.message.reply_text("❌ नाम खाली नहीं हो सकता। फिर से भेजो।")
        return WAITING_FOR_REPO_NAME

    # Get stored zip info
    pending = context.user_data.get('pending_zip')
    if not pending:
        await update.message.reply_text("❌ पहले ZIP फाइल भेजो!")
        return ConversationHandler.END

    await update.message.reply_text("⏳ प्रोसेस हो रहा है... कृपया रुको।")

    # Download the zip
    file_id = pending['file_id']
    file = await context.bot.get_file(file_id)
    zip_path = os.path.join(TEMP_DIR, pending['file_name'])
    await file.download_to_drive(zip_path)

    # Extract
    extract_path = os.path.join(TEMP_DIR, "extracted_" + str(pending['user_id']))
    os.makedirs(extract_path, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Unzip error: {str(e)}")
        shutil.rmtree(extract_path, ignore_errors=True)
        os.remove(zip_path)
        context.user_data.pop('pending_zip', None)
        return ConversationHandler.END

    # Remove single top-level folder if exists
    items = os.listdir(extract_path)
    if len(items) == 1 and os.path.isdir(os.path.join(extract_path, items[0])):
        top_folder = items[0]
        await update.message.reply_text(f"📁 एकल शीर्ष फोल्डर ('{top_folder}') हटाया जा रहा है...")
        top_path = os.path.join(extract_path, top_folder)
        for item in os.listdir(top_path):
            shutil.move(os.path.join(top_path, item), extract_path)
        os.rmdir(top_path)

    # GitHub authentication and repo creation
    g = Github(GITHUB_TOKEN)
    user_obj = g.get_user()

    # Check if repo already exists – if yes, append number
    base_name = repo_name
    attempt = 0
    while True:
        try:
            user_obj.get_repo(base_name)
            # exists – increment
            attempt += 1
            base_name = f"{repo_name}-{attempt}"
        except GithubException as e:
            if e.status == 404:
                break  # not found, we can create
            else:
                await update.message.reply_text(f"❌ GitHub check error: {e.data.get('message', str(e))}")
                shutil.rmtree(extract_path, ignore_errors=True)
                os.remove(zip_path)
                context.user_data.pop('pending_zip', None)
                return ConversationHandler.END

    await update.message.reply_text(f"🏗️ '{base_name}' बन रहा है...")
    try:
        repo = user_obj.create_repo(base_name, private=False, auto_init=False)
        await update.message.reply_text(f"✅ Repo बन गया: {repo.html_url}")
        upload_status = await upload_folder_to_github(repo, extract_path)
        await update.message.reply_text(f"📤 {upload_status}\n🔗 {repo.html_url}")
    except GithubException as e:
        await update.message.reply_text(f"❌ GitHub Error: {e.data.get('message', str(e))}")
    except Exception as e:
        await update.message.reply_text(f"❌ Unexpected: {str(e)}")
    finally:
        shutil.rmtree(extract_path, ignore_errors=True)
        os.remove(zip_path)
        context.user_data.pop('pending_zip', None)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ ऑपरेशन रद्द किया गया।")
    context.user_data.pop('pending_zip', None)
    return ConversationHandler.END

# ------------------------------------------------------------------
# Bot startup – with conversation handler
# ------------------------------------------------------------------
def run_bot():
    app = Application.builder().token(TOKEN).build()

    # Conversation handler for zip → name → upload
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, handle_zip)],
        states={
            WAITING_FOR_REPO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_repo_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    # Drop pending updates to avoid conflict
    app.bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Bot चल रहा है...")
    app.run_polling(drop_pending_updates=True)

# ------------------------------------------------------------------
# Main – start Flask in thread, then bot in main thread
# ------------------------------------------------------------------
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
