# bot.py
import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
from dotenv import load_dotenv
from keep_alive import keep_alive  # seu keep_alive.py separado

load_dotenv()

# ========== CONFIG ==========
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN env var")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

DATA_FILE = "dados.json"
ICON_URL = "https://cdn.discordapp.com/icons/1316463004618985522/c4766c485842022b18beda93d48dcd5b.png?size=2048"

# Categories (conforme solicitado)
CATEGORIA_STUMBLE = 1436161336890626068
CATEGORIA_VALORANT = 1437454774961438780

# Staff IDs
STAFF_IDS = [1229903485265383547, 852903512488280114]

# Jogos permitidos
JOGOS_PERMITIDOS = ["stumble", "valorant"]

# Mapas Stumble (fixos)
STUMBLE_MAPS = ["Block Dash", "Rush Hour", "Laser Tracer"]

# Taxa fixa por AP (R$)
TAXA_AP = 1.00

# ========== DATA IO ==========
data_lock = asyncio.Lock()

def _ensure_file():
    if not os.path.exists(DATA_FILE):
        base = {
            "filas": {},  # keyed by channel_id -> dict of queues per map/mode
            "ranking_stumble": {},
            "ranking_valorant": {}
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=4, ensure_ascii=False)

def read_data():
    _ensure_file()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def write_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=4, ensure_ascii=False)

# ========== HELPERS ==========
def key_from_name(name: str) -> str:
    return name.lower().replace(" ", "_").replace("x", "x")

def make_queue_embed_single(jogo: str, fila_record: dict, map_key: str):
    """Embed para uma fila específica (map/mode)"""
    mapa_label = fila_record["queues"][map_key]["label"]
    inscritos = fila_record["queues"][map_key]["inscritos"]
    max_p = fila_record["queues"][map_key]["max_pessoas"]
    rodadas = fila_record.get("rodadas", 1)
    desc = (
        f"🎮 **Jogo:** {jogo.capitalize()}\n"
        f"🗂️ **Modo/Mapa:** {mapa_label}\n"
        f"💰 **Valor por pessoa:** R$ {fila_record['valor']:.2f}\n"
        f"🧾 **Taxa:** R$ {TAXA_AP:.2f} por AP\n"
        f"👥 **{len(inscritos)}/{max_p} inscritos**\n\n"
    )
    if inscritos:
        desc += "🎟️ Jogadores:\n" + "\n".join(f"<@{uid}>" for uid in inscritos)
    else:
        desc += "Nenhum jogador na fila ainda."
    emb = discord.Embed(
        title=f"🎮 Fila #{rodadas} • {mapa_label}",
        description=desc,
        color=discord.Color.dark_red()
    )
    emb.set_thumbnail(url=ICON_URL)
    emb.set_footer(text="ZN Revelation • Apostas automáticas")
    return emb

async def pin_unpin_prev(channel: discord.TextChannel, new_msg: discord.Message, fila_record: dict):
    prev = fila_record.get("message_id")
    if prev:
        try:
            prev_msg = await channel.fetch_message(prev)
            if prev_msg and prev_msg.pinned:
                await prev_msg.unpin()
        except discord.NotFound:
            pass
    try:
        await new_msg.pin()
    except Exception:
        pass

def ensure_channel_fila_structure(d: dict, channel_id: str):
    """Garante que exista estrutura para esse canal em dados"""
    if "filas" not in d:
        d["filas"] = {}
    if channel_id not in d["filas"]:
        d["filas"][channel_id] = {
            "jogo": "stumble",  # default until set by command
            "valor": 1.0,
            "rodadas": 1,
            "queues": {},  # map_key -> { label, inscritos:list, max_pessoas:int, message_id }
        }

# ========== VIEWS & COMPONENTS ==========
class MapButtonsView(discord.ui.View):
    def __init__(self, channel_id: str, jogo: str):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.jogo = jogo  # 'stumble' or 'valorant'

        # add map buttons dynamically according to stored queues for this channel
        d = read_data()
        ensure_channel_fila_structure(d, channel_id)
        queues = d["filas"][channel_id]["queues"]
        for map_key, info in queues.items():
            # create a button per map (label visible)
            btn = discord.ui.Button(label=info["label"], style=discord.ButtonStyle.primary, custom_id=f"mapbtn|{channel_id}|{map_key}")
            self.add_item(btn)
        # add a general 'ver filas' button:
        self.add_item(discord.ui.Button(label="Ver filas", style=discord.ButtonStyle.secondary, custom_id=f"verfilas|{channel_id}"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # allow everyone to click
        return True

    # Since buttons were added dynamically, we listen to on_interaction using a global handler below
    # (discord.py doesn't let dynamic callbacks be set easily here)

# We'll register a global interaction handler for custom_id patterns:
@bot.event
async def on_interaction(interaction: discord.Interaction):
    # handle button custom_id patterns:
    if not interaction.type == discord.InteractionType.component:
        return

    cid = interaction.channel.id
    uid = str(interaction.user.id)
    custom_id = interaction.data.get("custom_id", "")
    # patterns:
    # mapbtn|<channel_id>|<map_key>
    # verfilas|<channel_id>
    # join|<channel_id>|<map_key>
    # leave|<channel_id>|<map_key>
    try:
        parts = custom_id.split("|")
        if parts[0] == "mapbtn" and parts[1] == str(cid):
            # user pressed the map button: show the queue embed and temporary join/leave buttons
            channel_id = parts[1]
            map_key = parts[2]
            d = read_data()
            fila = d["filas"].get(channel_id)
            if not fila:
                await interaction.response.send_message("⚠️ Esta fila não existe mais.", ephemeral=True)
                return
            # create a view with Join/Leave/Ver buttons specific to this map
            view = discord.ui.View(timeout=None)
            join_btn = discord.ui.Button(label="Entrar", style=discord.ButtonStyle.success, custom_id=f"join|{channel_id}|{map_key}")
            leave_btn = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger, custom_id=f"leave|{channel_id}|{map_key}")
            ver_btn = discord.ui.Button(label="Ver jogadores", style=discord.ButtonStyle.secondary, custom_id=f"ver|{channel_id}|{map_key}")
            view.add_item(join_btn)
            view.add_item(leave_btn)
            view.add_item(ver_btn)
            embed = make_queue_embed_single(fila["jogo"], fila, map_key)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

        if parts[0] == "join":
            channel_id = parts[1]
            map_key = parts[2]
            async with data_lock:
                d = read_data()
                fila = d["filas"].get(channel_id)
                if not fila:
                    await interaction.response.send_message("⚠️ Fila inexistente.", ephemeral=True)
                    return
                queue = fila["queues"].get(map_key)
                if not queue:
                    await interaction.response.send_message("⚠️ Fila desse mapa não existe.", ephemeral=True)
                    return
                if uid in queue["inscritos"]:
                    await interaction.response.send_message("⚠️ Você já está nessa fila.", ephemeral=True)
                    return
                if len(queue["inscritos"]) >= queue["max_pessoas"]:
                    await interaction.response.send_message("❌ Essa fila já está cheia.", ephemeral=True)
                    return
                queue["inscritos"].append(uid)
                # update ranking
                if fila["jogo"] == "stumble":
                    rk = d.setdefault("ranking_stumble", {})
                    rk[uid] = rk.get(uid, 0) + 1
                else:
                    rk = d.setdefault("ranking_valorant", {})
                    rk[uid] = rk.get(uid, 0) + 1
                write_data(d)

            # update pinned message if exists
            try:
                ch = bot.get_channel(int(channel_id))
                mid = fila.get("message_id")
                if ch and mid:
                    try:
                        m = await ch.fetch_message(mid)
                        await m.edit(embed=make_queue_embed_single(fila["jogo"], fila, map_key), view=MapButtonsView(channel_id, fila["jogo"]))
                    except Exception:
                        pass
            except Exception:
                pass

            await interaction.response.send_message(f"✅ Você entrou na fila **{fila['queues'][map_key]['label']}**!", ephemeral=True)

            # If reached capacity -> create ticket
            # Re-read to ensure up-to-date
            async with data_lock:
                d2 = read_data()
                fila2 = d2["filas"].get(channel_id)
                queue2 = fila2["queues"].get(map_key)
                if len(queue2["inscritos"]) >= queue2["max_pessoas"]:
                    # create ticket channel in appropriate category
                    guild = interaction.guild
                    if fila2["jogo"] == "stumble":
                        categoria = discord.utils.get(guild.categories, id=CATEGORIA_STUMBLE)
                    else:
                        categoria = discord.utils.get(guild.categories, id=CATEGORIA_VALORANT)
                    chan_name = f"🎫-{queue2['label'].lower().replace(' ', '')}-apostado"
                    try:
                        new_chan = await guild.create_text_channel(name=chan_name, category=categoria)
                    except Exception:
                        # fallback to default category
                        new_chan = await guild.create_text_channel(name=chan_name)
                    # ping users
                    users = [guild.get_member(int(x)) for x in queue2["inscritos"]]
                    mentions = " ".join(u.mention for u in users if u)
                    total_value = queue2["max_pessoas"] * fila2["valor"]
                    taxa_total = queue2["max_pessoas"] * TAXA_AP
                    # announcement + embed
                    await new_chan.send(f"🎮 {mentions} — o confronto foi criado! Enviem o **comprovante do Pix** aqui.")
                    emb = discord.Embed(
                        title=f"🎟️ Ticket • {queue2['label']}",
                        description=(
                            f"💸 **Valor por pessoa:** R$ {fila2['valor']:.2f}\n"
                            f"💰 **Total (sem taxa):** R$ {total_value:.2f}\n"
                            f"💸 **Taxa total (R$ {TAXA_AP:.2f} por AP):** R$ {taxa_total:.2f}\n\n"
                            f"🔑 **Chave Pix:** `https://tipa.ai/duckioo23`\n\n"
                            f"⚠️ Enviem o comprovante **neste canal** para começar a partida."
                        ),
                        color=discord.Color.dark_red()
                    )
                    emb.set_thumbnail(url=ICON_URL)
                    await new_chan.send(embed=emb)
                    # reset this queue
                    queue2["inscritos"] = []
                    fila2["rodadas"] = fila2.get("rodadas", 1) + 1
                    write_data(d2)
                    # update pinned message in original channel
                    try:
                        if fila2.get("message_id"):
                            och = bot.get_channel(int(channel_id))
                            if och:
                                om = await och.fetch_message(fila2.get("message_id"))
                                await om.edit(embed=make_queue_embed_single(fila2["jogo"], fila2, map_key), view=MapButtonsView(channel_id, fila2["jogo"]))
                    except Exception:
                        pass
                    # notify the user who triggered
                    try:
                        await interaction.followup.send("✅ Fila completa! Ticket criado e fila reiniciada.", ephemeral=True)
                    except Exception:
                        pass
            return

        if parts[0] == "leave":
            channel_id = parts[1]
            map_key = parts[2]
            async with data_lock:
                d = read_data()
                fila = d["filas"].get(channel_id)
                if not fila:
                    await interaction.response.send_message("⚠️ Fila inexistente.", ephemeral=True)
                    return
                queue = fila["queues"].get(map_key)
                if not queue:
                    await interaction.response.send_message("⚠️ Fila desse mapa não existe.", ephemeral=True)
                    return
                if uid not in queue["inscritos"]:
                    await interaction.response.send_message("⚠️ Você não está nessa fila.", ephemeral=True)
                    return
                queue["inscritos"].remove(uid)
                write_data(d)
            # update pinned message
            try:
                ch = bot.get_channel(int(channel_id))
                mid = fila.get("message_id")
                if ch and mid:
                    m = await ch.fetch_message(mid)
                    await m.edit(embed=make_queue_embed_single(fila["jogo"], fila, map_key), view=MapButtonsView(channel_id, fila["jogo"]))
            except Exception:
                pass
            await interaction.response.send_message("🚪 Você saiu da fila.", ephemeral=True)
            return

        if parts[0] == "ver":
            channel_id = parts[1]
            map_key = parts[2]
            d = read_data()
            fila = d["filas"].get(channel_id)
            if not fila:
                await interaction.response.send_message("⚠️ Fila inexistente.", ephemeral=True)
                return
            queue = fila["queues"].get(map_key)
            if not queue or not queue["inscritos"]:
                await interaction.response.send_message("Nenhum jogador nessa fila.", ephemeral=True)
                return
            mentions = "\n".join(f"<@{x}>" for x in queue["inscritos"])
            await interaction.response.send_message(f"👥 Jogadores na fila {queue['label']}:\n{mentions}", ephemeral=True)
            return

        if parts[0] == "verfilas":
            channel_id = parts[1]
            d = read_data()
            fila = d["filas"].get(channel_id)
            if not fila:
                await interaction.response.send_message("⚠️ Não há filas nesse canal.", ephemeral=True)
                return
            lines = []
            for map_key, q in fila["queues"].items():
                lines.append(f"**{q['label']}** — {len(q['inscritos'])}/{q['max_pessoas']}")
            await interaction.response.send_message("📋 Filas:\n" + "\n".join(lines), ephemeral=True)
            return
    except Exception:
        # fallback: ignore unknown custom_id structures
        return

# ========== COMMANDS ==========

# /criar -> Stumble Guys (fixo maps)
@bot.tree.command(name="criar", description="Cria o painel de Stumble Guys com mapas (Block Dash, Rush Hour, Laser Tracer).")
@app_commands.describe(valor="Valor da aposta por pessoa (R$)", max_pessoas="Número máximo por fila")
async def criar(interaction: discord.Interaction, valor: float = 1.0, max_pessoas: int = 8):
    if interaction.user.id not in STAFF_IDS:
        return await interaction.response.send_message("❌ Você não tem permissão para criar filas.", ephemeral=True)
    if max_pessoas < 2 or max_pessoas > 128:
        return await interaction.response.send_message("❌ max_pessoas deve ser entre 2 e 128.", ephemeral=True)

    channel = interaction.channel
    cid = str(channel.id)
    async with data_lock:
        d = read_data()
        ensure_channel_fila_structure(d, cid)
        fila = d["filas"][cid]
        fila["jogo"] = "stumble"
        fila["valor"] = float(valor)
        fila["rodadas"] = fila.get("rodadas", 1)
        # ensure queues for the 3 maps exist (keep previous configs if present)
        for m in STUMBLE_MAPS:
            k = key_from_name(m)
            if k not in fila["queues"]:
                fila["queues"][k] = {"label": m, "inscritos": [], "max_pessoas": max_pessoas, "message_id": None}
            else:
                # update max_pessoas default if currently empty
                fila["queues"][k]["max_pessoas"] = max_pessoas
        write_data(d)

    # create a message with MapButtonsView
    view = MapButtonsView(cid, "stumble")
    # create a simple embed showing the maps and counts
    desc = "Painel de mapas:\n\n" + "\n".join(f"• {m}" for m in STUMBLE_MAPS)
    embed = discord.Embed(title="🎮 Painel • Stumble Guys", description=desc, color=discord.Color.dark_red())
    msg = await channel.send(embed=embed, view=view)

    # pin/unpin and store message_id
    async with data_lock:
        d = read_data()
        fila = d["filas"][cid]
        await pin_unpin_prev(channel, msg, fila)
        fila["message_id"] = msg.id
        write_data(d)

    # register view persistently
    try:
        bot.add_view(MapButtonsView(cid, "stumble"), message_id=msg.id)
    except Exception:
        bot.add_view(MapButtonsView(cid, "stumble"))

    await interaction.response.send_message("✅ Painel de Stumble criado e fixado!", ephemeral=True)


# /criarvalorant -> Valorant with custom modes (comma-separated)
@bot.tree.command(name="criarvalorant", description="Cria painel Valorant com modos customizáveis (ex: 1x1,2x2,5x5).")
@app_commands.describe(valor="Valor da aposta por pessoa (R$)", max_pessoas="Número máximo padrão por fila", modos="Modos separados por vírgula. ex: 1x1,2x2,5x5")
async def criarvalorant(interaction: discord.Interaction, valor: float = 1.0, max_pessoas: int = 5, modos: str = "1x1,2x2,5x5"):
    if interaction.user.id not in STAFF_IDS:
        return await interaction.response.send_message("❌ Você não tem permissão para criar filas.", ephemeral=True)
    channel = interaction.channel
    cid = str(channel.id)
    mode_list = [m.strip() for m in modos.split(",") if m.strip()]
    if not mode_list:
        return await interaction.response.send_message("❌ Forneça ao menos 1 modo.", ephemeral=True)
    async with data_lock:
        d = read_data()
        ensure_channel_fila_structure(d, cid)
        fila = d["filas"][cid]
        fila["jogo"] = "valorant"
        fila["valor"] = float(valor)
        fila["rodadas"] = fila.get("rodadas", 1)
        # ensure queues for each mode
        for m in mode_list:
            k = key_from_name(m)
            if k not in fila["queues"]:
                fila["queues"][k] = {"label": m, "inscritos": [], "max_pessoas": max_pessoas, "message_id": None}
            else:
                fila["queues"][k]["max_pessoas"] = max_pessoas
        write_data(d)

    view = MapButtonsView(cid, "valorant")
    desc = "Painel de modos Valorant:\n\n" + "\n".join(f"• {m}" for m in mode_list)
    embed = discord.Embed(title="🎮 Painel • Valorant", description=desc, color=discord.Color.dark_red())
    msg = await channel.send(embed=embed, view=view)

    async with data_lock:
        d = read_data()
        fila = d["filas"][cid]
        await pin_unpin_prev(channel, msg, fila)
        fila["message_id"] = msg.id
        write_data(d)

    # persist view
    try:
        bot.add_view(MapButtonsView(cid, "valorant"), message_id=msg.id)
    except Exception:
        bot.add_view(MapButtonsView(cid, "valorant"))

    await interaction.response.send_message(f"✅ Painel de Valorant criado e fixado! Modos: {', '.join(mode_list)}", ephemeral=True)

# ranking commands
@bot.tree.command(name="ranking", description="Mostra ranking geral (especifique jogo: stumble ou valorant)")
@app_commands.describe(jogo="stumble ou valorant")
async def ranking(interaction: discord.Interaction, jogo: str = "stumble"):
    d = read_data()
    if jogo.lower() == "stumble":
        rk = d.get("ranking_stumble", {})
    else:
        rk = d.get("ranking_valorant", {})
    if not rk:
        return await interaction.response.send_message("Nenhum dado de ranking ainda.", ephemeral=True)
    top = sorted(rk.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"<@{uid}> — {count} entradas" for uid, count in top]
    emb = discord.Embed(title=f"🏆 Ranking • {jogo.capitalize()}", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=emb, ephemeral=False)

# remover painel/fila do canal
@bot.tree.command(name="remover", description="Remove painel/fila ativa neste canal (desfixa mensagem e limpa filas).")
async def remover(interaction: discord.Interaction):
    if interaction.user.id not in STAFF_IDS:
        return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
    channel = interaction.channel
    cid = str(channel.id)
    async with data_lock:
        d = read_data()
        filas = d.get("filas", {})
        if cid not in filas:
            return await interaction.response.send_message("⚠️ Não há painel neste canal.", ephemeral=True)
        fila = filas[cid]
        # unpin message if exists
        mid = fila.get("message_id")
        if mid:
            try:
                msg = await channel.fetch_message(mid)
                if msg.pinned:
                    await msg.unpin()
            except Exception:
                pass
        # remove structure
        del filas[cid]
        write_data(d)
    await interaction.response.send_message("🗑️ Painel removido e dados limpos.", ephemeral=False)

# ========== STARTUP ==========
@bot.event
async def on_ready():
    print(f"✅ Bot online como {bot.user}")
    try:
        await bot.tree.sync()
        print("Slash commands sincronizados.")
    except Exception as e:
        print("Erro ao sincronizar comandos:", e)

    # restore persistent views for messages stored
    d = read_data()
    for ch_id, fila in d.get("filas", {}).items():
        mid = fila.get("message_id")
        if mid:
            try:
                bot.add_view(MapButtonsView(ch_id, fila.get("jogo", "stumble")), message_id=mid)
                print(f"Restored view for channel {ch_id} message {mid}")
            except Exception:
                try:
                    bot.add_view(MapButtonsView(ch_id, fila.get("jogo", "stumble")))
                except Exception:
                    pass

# ========== RUN ==========
if __name__ == "__main__":
    keep_alive()  # seu keep_alive.py roda Flask em background
    bot.run(TOKEN)
