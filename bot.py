import os
import re
import logging
from datetime import datetime, date
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config ---
TOKEN = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", 10000))

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Priority Tag Values ---
TAG_VALUES = {"FGR": 3, "CETS": 3, "CS": 2}

# --- Helpers ---

def parse_task(text: str) -> dict:
    """Parse: Tarea @tag |eProyecto |pU2 |f120226"""
    raw = text.strip()
    tag = None
    project = None
    priority_str = None
    date_str = None

    # Extract @tag
    m = re.search(r"@(\S+)", raw)
    if m:
        tag = m.group(1).upper()
        raw = raw[:m.start()] + raw[m.end():]

    # Extract |e project
    m = re.search(r"\|e\s*(\S+)", raw, re.IGNORECASE)
    if m:
        project = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    # Extract |p priority (e.g. U2, N3, U1)
    m = re.search(r"\|p\s*([UuNn])\s*(\d)", raw)
    if m:
        priority_str = m.group(1).upper() + m.group(2)
        raw = raw[:m.start()] + raw[m.end():]

    # Extract |f date (ddmmyy)
    m = re.search(r"\|f\s*(\d{6})", raw)
    if m:
        date_str = m.group(1)
        raw = raw[:m.start()] + raw[m.end():]

    title = re.sub(r"\s+", " ", raw).strip()
    return {"title": title, "tag": tag, "project": project, "priority_str": priority_str, "date_str": date_str}


def calc_date_value(date_str: str | None) -> int:
    if not date_str:
        return 0
    try:
        d = datetime.strptime(date_str, "%d%m%y").date()
        delta = (d - date.today()).days
        if delta <= 0:
            return 3  # overdue = max urgency
        if delta <= 3:
            return 3
        if delta <= 15:
            return 2
        return 1
    except ValueError:
        return 0


def calc_priority_value(priority_str: str | None) -> int:
    if not priority_str:
        return 0
    urgency = 1 if priority_str[0] == "U" else -1
    num = int(priority_str[1])
    return urgency + num


def calc_total_score(tag: str | None, priority_str: str | None, date_str: str | None) -> int:
    tag_val = TAG_VALUES.get(tag, 0) if tag else 0
    return tag_val + calc_priority_value(priority_str) + calc_date_value(date_str)


def next_available_id(user_id: int) -> int:
    """Find smallest available ID (1-99) for user."""
    rows = sb.table("tasks").select("task_id").eq("user_id", user_id).execute().data
    used = {r["task_id"] for r in rows}
    for i in range(1, 100):
        if i not in used:
            return i
    return 99


def format_date(date_str: str | None) -> str:
    if not date_str:
        return ""
    try:
        d = datetime.strptime(date_str, "%d%m%y").date()
        return d.strftime("%d/%m/%y")
    except ValueError:
        return date_str


def format_task(t: dict, idx: int) -> str:
    """Format a task for display. idx = visual position."""
    parts = [f"{idx}. {t['title']}"]
    if t.get("tag"):
        parts.append(f"@{t['tag']}")
    if t.get("project"):
        parts.append(f"|e {t['project']}")
    if t.get("priority_str"):
        parts.append(f"|p {t['priority_str']}")
    if t.get("date_str"):
        parts.append(f"📅{format_date(t['date_str'])}")
    return " ".join(parts)


def get_tasks(user_id: int, tag: str = None, project: str = None) -> list[dict]:
    """Get tasks sorted by score desc."""
    q = sb.table("tasks").select("*").eq("user_id", user_id).eq("done", False)
    if tag:
        q = q.eq("tag", tag.upper())
    if project:
        q = q.ilike("project", f"%{project}%")
    rows = q.execute().data

    # Recalculate scores (date value changes daily)
    for r in rows:
        r["_score"] = calc_total_score(r.get("tag"), r.get("priority_str"), r.get("date_str"))
    rows.sort(key=lambda x: x["_score"], reverse=True)
    return rows


# --- Handlers ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗂 *TaskBot*\n\n"
        "Escribe una tarea directamente:\n"
        "`Revisar doc @FGR |e Sellout |pU2 |f150226`\n\n"
        "*Comandos:*\n"
        "/show — ver todas las tareas\n"
        "/show @TAG — filtrar por etiqueta\n"
        "/show p PROYECTO — filtrar por proyecto\n"
        "/done ID — marcar como hecha\n"
        "/del ID — eliminar tarea\n"
        "/edit ID campo valor — editar tarea\n"
        "/undo — restaurar última acción\n"
        "/help — ayuda completa",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Guía Rápida*\n\n"
        "*Crear tarea:* escribe texto directo\n"
        "`Lavar ropa |pU2`\n"
        "`Llamar doctor @CS |f200226`\n"
        "`Informe final @FGR |e Sellout |pN1 |f280226`\n\n"
        "*Campos opcionales (cualquier orden):*\n"
        "• `@TAG` → etiqueta (FGR=3, CETS=3, CS=2)\n"
        "• `|e nombre` → proyecto\n"
        "• `|p U/N + 1-3` → prioridad\n"
        "• `|f ddmmyy` → fecha\n\n"
        "*Comandos:*\n"
        "`/show` — todas las tareas\n"
        "`/show @FGR` — por etiqueta\n"
        "`/show p Sellout` — por proyecto\n"
        "`/done 3` — completar tarea ID 3\n"
        "`/del 5` — eliminar tarea ID 5\n"
        "`/edit 3 title Nuevo título`\n"
        "`/edit 3 tag CS`\n"
        "`/edit 3 project NuevoProj`\n"
        "`/edit 3 priority U3`\n"
        "`/edit 3 date 150326`\n"
        "`/undo` — deshacer última acción\n"
        "`/done all` — completar todas",
        parse_mode="Markdown"
    )


async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = ctx.args or []
    tag = project = None

    if args:
        text = " ".join(args)
        if text.startswith("@"):
            tag = text[1:]
        elif text.lower().startswith("p "):
            project = text[2:].strip()
        else:
            project = text  # fallback: treat as project search

    tasks = get_tasks(user_id, tag=tag, project=project)
    if not tasks:
        label = ""
        if tag:
            label = f" con @{tag.upper()}"
        elif project:
            label = f" en proyecto '{project}'"
        await update.message.reply_text(f"📭 No hay tareas{label}.")
        return

    lines = []
    header = "📋 *Tareas*"
    if tag:
        header += f" @{tag.upper()}"
    if project:
        header += f" — {project}"
    lines.append(header)
    lines.append("")

    for i, t in enumerate(tasks, 1):
        lines.append(format_task(t, i))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Uso: `/done ID`", parse_mode="Markdown")
        return

    if ctx.args[0].lower() == "all":
        tasks = get_tasks(user_id)
        if not tasks:
            await update.message.reply_text("📭 No hay tareas pendientes.")
            return
        for t in tasks:
            sb.table("tasks").update({"done": True}).eq("id", t["id"]).execute()
        # Store undo
        ctx.bot_data[f"undo_{user_id}"] = {"action": "done_all", "task_ids": [t["id"] for t in tasks]}
        await update.message.reply_text(f"✅ {len(tasks)} tareas completadas.")
        return

    try:
        task_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID debe ser un número.")
        return

    row = sb.table("tasks").select("*").eq("user_id", user_id).eq("task_id", task_id).eq("done", False).execute().data
    if not row:
        await update.message.reply_text(f"❌ Tarea #{task_id} no encontrada.")
        return

    sb.table("tasks").update({"done": True}).eq("id", row[0]["id"]).execute()
    ctx.bot_data[f"undo_{user_id}"] = {"action": "done", "row_id": row[0]["id"]}
    await update.message.reply_text(f"✅ *{row[0]['title']}* completada.", parse_mode="Markdown")


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Uso: `/del ID`", parse_mode="Markdown")
        return

    try:
        task_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID debe ser un número.")
        return

    row = sb.table("tasks").select("*").eq("user_id", user_id).eq("task_id", task_id).eq("done", False).execute().data
    if not row:
        await update.message.reply_text(f"❌ Tarea #{task_id} no encontrada.")
        return

    sb.table("tasks").delete().eq("id", row[0]["id"]).execute()
    ctx.bot_data[f"undo_{user_id}"] = {"action": "delete", "data": row[0]}
    await update.message.reply_text(f"🗑 *{row[0]['title']}* eliminada.", parse_mode="Markdown")


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "Uso: `/edit ID campo valor`\n"
            "Campos: title, tag, project, priority, date",
            parse_mode="Markdown"
        )
        return

    try:
        task_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID debe ser un número.")
        return

    field = ctx.args[1].lower()
    value = " ".join(ctx.args[2:])

    row = sb.table("tasks").select("*").eq("user_id", user_id).eq("task_id", task_id).eq("done", False).execute().data
    if not row:
        await update.message.reply_text(f"❌ Tarea #{task_id} no encontrada.")
        return

    field_map = {
        "title": "title",
        "tag": "tag",
        "project": "project",
        "priority": "priority_str",
        "date": "date_str",
    }

    if field not in field_map:
        await update.message.reply_text(f"❌ Campo '{field}' no válido. Usa: title, tag, project, priority, date")
        return

    db_field = field_map[field]
    update_data = {db_field: value.upper() if field == "tag" else value}

    # Store undo
    ctx.bot_data[f"undo_{user_id}"] = {"action": "edit", "row_id": row[0]["id"], "field": db_field, "old_value": row[0][db_field]}

    sb.table("tasks").update(update_data).eq("id", row[0]["id"]).execute()
    await update.message.reply_text(f"✏️ Tarea #{task_id} actualizada: {field} → {value}")


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    undo = ctx.bot_data.pop(f"undo_{user_id}", None)
    if not undo:
        await update.message.reply_text("❌ Nada que deshacer.")
        return

    action = undo["action"]
    if action == "done":
        sb.table("tasks").update({"done": False}).eq("id", undo["row_id"]).execute()
        await update.message.reply_text("↩️ Tarea restaurada.")
    elif action == "done_all":
        for rid in undo["task_ids"]:
            sb.table("tasks").update({"done": False}).eq("id", rid).execute()
        await update.message.reply_text(f"↩️ {len(undo['task_ids'])} tareas restauradas.")
    elif action == "delete":
        data = undo["data"]
       data.pop("_score", None)
        sb.table("tasks").insert(data).execute()
        await update.message.reply_text(f"↩️ *{data['title']}* restaurada.", parse_mode="Markdown")
    elif action == "edit":
        sb.table("tasks").update({undo["field"]: undo["old_value"]}).eq("id", undo["row_id"]).execute()
        await update.message.reply_text("↩️ Edición revertida.")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Any non-command text = create task."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return

    parsed = parse_task(text)
    if not parsed["title"]:
        await update.message.reply_text("❌ La tarea necesita un título.")
        return

    task_id = next_available_id(user_id)

    record = {
        "user_id": user_id,
        "task_id": task_id,
        "title": parsed["title"],
        "tag": parsed["tag"],
        "project": parsed["project"],
        "priority_str": parsed["priority_str"],
        "date_str": parsed["date_str"],
        "done": False,
    }

    result = sb.table("tasks").insert(record).execute()
    ctx.bot_data[f"undo_{user_id}"] = {"action": "create", "row_id": result.data[0]["id"]}

    score = calc_total_score(parsed["tag"], parsed["priority_str"], parsed["date_str"])
    parts = [f"✅ *#{task_id}* {parsed['title']}"]
    if parsed["tag"]:
        parts.append(f"@{parsed['tag']}")
    if parsed["project"]:
        parts.append(f"📁{parsed['project']}")
    if parsed["priority_str"]:
        parts.append(f"⚡{parsed['priority_str']}")
    if parsed["date_str"]:
        parts.append(f"📅{format_date(parsed['date_str'])}")

    await update.message.reply_text(" ".join(parts), parse_mode="Markdown")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("show", "Ver tareas (filtros: @TAG, p PROYECTO)"),
        BotCommand("done", "Completar tarea por ID"),
        BotCommand("del", "Eliminar tarea por ID"),
        BotCommand("edit", "Editar tarea: /edit ID campo valor"),
        BotCommand("undo", "Deshacer última acción"),
        BotCommand("help", "Ayuda completa"),
    ])


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        logger.info(f"Starting webhook on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
        )
    else:
        logger.info("Starting polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
