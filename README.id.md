# 🤖 AegisX Agent

**Agentic AI Super-power** — Mendukung provider LLM apa pun, dengan tool calling, RAG, memori, perencanaan, dan persona yang bisa dikustomisasi.

> 🇬🇧 Versi resmi bahasa Inggris: [README.md](README.md)

## ✨ Fitur

| Fitur | Deskripsi |
|-------|-----------|
| 🔌 **LLM Multi-Provider** | OpenAI, Anthropic, Ollama (lokal), Groq, atau endpoint apa pun yang kompatibel OpenAI |
| 🔧 **Tool Calling** | Pencarian web, eksekusi kode, operasi file, shell, kalkulator, tanggal-waktu |
| 📚 **RAG** | Unggah dokumen, cari basis pengetahuan dengan ChromaDB |
| 🧠 **Memori** | Riwayat percakapan + penyimpanan fakta jangka panjang |
| 📋 **Perencanaan** | Penalaran multi-langkah dengan pola ReAct |
| 🎭 **Persona** | Persona bawaan + pembuatan persona kustom |
| 🖥️ **CLI Kaya** | Antarmuka terminal indah dengan streaming |
| 📁 **Sadar Workspace** | Tahu folder tempatnya berjalan: stack, status git, instruksi `AGENTS.md` |
| 🔐 **Gerbang Izin** | Setiap panggilan tool diklasifikasi berdasarkan risiko dan digate sebelum berjalan |
| ⏰ **Penjadwal** | Tugas gaya-cron tanpa pengawasan, dengan checkpoint, resume, exponential backoff, deteksi loop, dan log audit |
| 🧭 **Daemon persisten** | Tugas terjadwal tersimpan di SQLite dan bertahan melewati restart, dengan cron 5-field lengkap (rentang, step, nama) dan shutdown halus |
| 🧩 **Plugin** | Tool terversi yang dimuat eksplisit, dengan JSON Schema dan kebijakan izin |

## 🚀 Mulai Cepat

### Instalasi

Satu perintah — menginstall dari GitHub, menyiapkan virtualenv, dan menaruh `aegisx` di PATH:

```bash
curl -fsSL https://raw.githubusercontent.com/aegisxresearch/AegisX-Agent/main/installer.sh | bash
```

Opsi: `--ref <branch-atau-tag>`, `--dir <path>`, `--bin <path>`, `--with-mcp` (ekstra
MCP), `--dev` (tool test); `--uninstall` untuk menghapusnya. Jalankan `installer.sh
--help` untuk daftar lengkap. Menjalankan ulang installer berarti meng-update install
yang sudah ada.

Installer juga membersihkan sisa instalasi manual lama: baris `alias aegisx=...`
atau `export PATH="…aegisx…/bin:$PATH"` yang usang di `~/.bashrc`,
`~/.bash_aliases`, `~/.profile`, atau `~/.zshrc` akan membayangi perintah baru
dengan error "No such file or directory" begitu direktori tujuannya hilang. Hanya
baris yang terbukti mati yang dihapus — komentar, entri yang masih valid, dan baris
PATH milik installer sendiri tetap dipertahankan — dan setiap file yang diedit
dibackup dulu sebagai `.bak-aegisx-*` bertimestamp. Pembersihan yang sama juga
berjalan saat `--uninstall`.

Ingin mengembangkan dari sebuah checkout?

```bash
pip install -e ".[dev]"
```

### Jalankan di sebuah proyek

```bash
cd proyek-saya

# Chat interaktif — agen membaca folder ini, status git-nya, dan AGENTS.md
aegisx

# Sekali jalan: kerjakan tugas lalu keluar (bisa di-script)
aegisx run "tambahkan test untuk scheduler"
echo "kenapa CI gagal?" | aegisx run
```

Tidak perlu konfigurasi jika server **Ollama** sudah berjalan: AegisX
menemukannya otomatis, memilih model chat yang terpasang, dan langsung mulai.
Jika tidak, atur provider secara eksplisit (di bawah).

### Atur provider LLM Anda

```bash
# Opsi 1: OpenAI
export AEGISX_OPENAI_API_KEY="sk-..."

# Opsi 2: Anthropic
export AEGISX_LLM_PROVIDER=anthropic
export AEGISX_ANTHROPIC_API_KEY="sk-ant-..."

# Opsi 3: Ollama (lokal, gratis!)
export AEGISX_LLM_PROVIDER=ollama
export AEGISX_OLLAMA_MODEL=llama3.1

# Opsi 4: Groq (inferensi cepat)
export AEGISX_LLM_PROVIDER=groq
export AEGISX_GROQ_API_KEY="gsk_..."

# Opsi 5: Endpoint kustom apa pun yang kompatibel OpenAI
export AEGISX_LLM_PROVIDER=custom
export AEGISX_CUSTOM_BASE_URL="https://api.together.xyz/v1"
export AEGISX_CUSTOM_API_KEY="kunci-anda"
export AEGISX_CUSTOM_MODEL="meta-llama/Llama-3-70b-chat-hf"
```

### Yang dilihatnya saat mulai

```
  📁 proyek-saya (/home/anda/kode/proyek-saya)
  Python • git main, 3 berubah • 412 file
  📜 AGENTS.md dimuat sebagai instruksi
  🔌 ollama • 🧠 llama3.1 • 🔧 15 tools • 💡 2 skills • 🔐 ask • 🎭 default
```

Giliran pertama sudah tahu direktori kerja, bahasanya, apakah tree-nya kotor,
dan instruksi dari `AGENTS.md` / `CLAUDE.md` — jadi tidak perlu membuang satu
panggilan tool untuk mencari tahu. Matikan dengan
`AEGISX_PROJECT_CONTEXT_ENABLED=false`.

### Opsi chat

```bash
aegisx --provider ollama --model llama3.1
aegisx -p openai -m gpt-4o
aegisx -p custom --url https://api.together.xyz/v1 -k kunci-anda -m meta-llama/Llama-3-70b-chat-hf
```

## 📖 Perintah

```bash
aegisx                    # Mulai chat interaktif di folder saat ini
aegisx chat               # Sama seperti di atas
aegisx run "tugas"        # Sekali jalan: kerjakan, cetak jawaban, keluar
aegisx run < tugas.md     # Tugas dibaca dari stdin (ramah pipeline)
aegisx plan "tujuan"      # Rencanakan dan eksekusi tujuan multi-langkah
aegisx ingest ./docs/     # Unggah dokumen ke basis pengetahuan
aegisx search "query"     # Cari basis pengetahuan
aegisx personas           # Daftar persona yang tersedia
aegisx tools              # Daftar tool beserta tingkat risikonya
aegisx config-info        # Tampilkan konfigurasi saat ini

# Keamanan
aegisx chat --permission-mode read-only   # Hanya tool read-only yang boleh
aegisx chat --permission-mode allow-all   # Tanpa gate (tetap diaudit)

# Tugas terjadwal (berjalan otomatis)
aegisx schedule add nightly "ringkas inbox saya" --interval 1h
aegisx schedule add laporan "tulis laporan mingguan" --daily 09:00
aegisx schedule list      # Tugas, jadwal berikutnya, status terakhir
aegisx schedule run       # Eksekusi tugas yang jatuh tempo sekarang
aegisx schedule run --once  # Tembakkan semua yang jatuh tempo lalu keluar
aegisx schedule logs <id> # Riwayat eksekusi satu tugas
aegisx schedule cancel <id> # Pause tugas sekarang (menghentikan run aktif)
aegisx schedule resume <id> # Lanjutkan tugas terpause dari checkpoint
aegisx schedule checkpoint <id> # Tampilkan checkpoint terakhir tugas
aegisx schedule remove <id>

# Plugin (dimuat eksplisit, sadar-izin)
aegisx plugin list         # Plugin termuat + putusan gate untuk masing-masing
aegisx plugin load ./plugin_saya.py # Muat dari file Python
aegisx plugin load paket_saya.plugins # Muat dari module yang bisa diimpor
aegisx plugin unload demo  # Hapus plugin yang termuat

# Server MCP (Model Context Protocol — katalog tool eksternal apa pun)
aegisx mcp list                     # Server terkonfigurasi + status koneksi
aegisx mcp connect filesystem       # Daftarkan tool server sebagai plugin bergerbang
aegisx mcp disconnect filesystem    # Hapus tool-nya, tutup sesi
aegisx mcp add files npx -y @modelcontextprotocol/server-filesystem /tmp
aegisx mcp remove files
```

## 🔌 Provider yang Didukung

| Provider | Cara setup | Gratis? |
|----------|-----------|---------|
| **OpenAI** | `AEGISX_OPENAI_API_KEY` | ❌ (berbayar) |
| **Anthropic** | `AEGISX_ANTHROPIC_API_KEY` | ❌ (berbayar) |
| **Ollama** | Install ollama, tarik model | ✅ (lokal) |
| **Groq** | `AEGISX_GROQ_API_KEY` | ✅ (free tier) |
| **Kustom** | `AEGISX_CUSTOM_BASE_URL` | Tergantung |

### Contoh Provider Kustom

Bekerja dengan API apa pun yang kompatibel OpenAI:

```bash
# Together AI
aegisx -p custom -u https://api.together.xyz/v1 -k $TOGETHER_KEY -m meta-llama/Llama-3-70b-chat-hf

# OpenRouter
aegisx -p custom -u https://openrouter.ai/api/v1 -k $OPENROUTER_KEY -m anthropic/claude-3.5-sonnet

# LM Studio lokal
aegisx -p custom -u http://localhost:1234/v1 -m model-lokal

# vLLM
aegisx -p custom -u http://localhost:8000/v1 -m nama-model

# Text Generation WebUI
aegisx -p custom -u http://localhost:5000/v1 -m nama-model
```

## 🎭 Persona

```bash
# Daftar persona
aegisx personas

# Gunakan persona
aegisx --persona coder
aegisx --persona researcher

# Buat persona kustom
# Simpan ke ~/.aegisx/personas/persona_saya.txt
```

Persona bawaan: `default`, `coder`, `researcher`, `analyst`, `creative`, `hacker`, `scientist`

## 🔧 Tool

| Tool | Risiko | Deskripsi |
|------|--------|-----------|
| `web_search` | safe | Cari internet via DuckDuckGo |
| `execute_code` | dangerous | Jalankan Python di proses ini (tanpa sandbox) |
| `file_ops` | bervariasi | Baca, tulis, daftar, cari, hapus file |
| `shell` | dangerous | Jalankan perintah shell (opt-in) |
| `calculator` | safe | Evaluasi ekspresi matematika |
| `datetime` | safe | Utilitas tanggal/waktu |
| `rag_search` | safe | Cari basis pengetahuan dokumen |
| `skill` | safe | Daftar, cari, dan muat skill yang bisa dipakai ulang |
| `api_call` | bervariasi | Panggil endpoint REST apa pun |
| `db_query` | bervariasi | Kueri SQLite / PostgreSQL |
| `web_scrape` | safe | Scrape dan ekstrak konten halaman |
| `codebase` | safe | Jelajahi struktur dan kode proyek |
| `code_edit` | caution | Edit file dengan pratinjau diff |
| `git` | bervariasi | Git status / diff / commit / branch |
| `run_tests` | bervariasi | Deteksi otomatis dan jalankan test suite |

## 🔐 Izin (Permissions)

Setiap panggilan tool melewati satu gerbang sebelum menyentuh sistem. Tool
mendeklarasikan risikonya sendiri, dan dievaluasi **per panggilan** —
`file_ops read` aman, `file_ops delete` tidak.

| Risiko | Contoh | Yang dilakukan mode `ask` |
|--------|--------|---------------------------|
| `safe` | baca file, cari, hitung, `git status` | langsung jalan |
| `caution` | tulis/edit file, POST/PUT, jalankan test | jalan, dan diaudit |
| `dangerous` | `execute_code`, `shell`, hapus file, `git commit`, `DELETE` | minta izin dulu |

| Mode | Perilaku |
|------|----------|
| `ask` (bawaan) | panggilan safe dan caution jalan; dangerous minta izin |
| `read-only` | hanya panggilan safe yang jalan |
| `allow-all` | tidak ada yang digate (tetap semua diaudit) |

```bash
aegisx chat --permission-mode read-only
AEGISX_PERMISSION_MODE=read-only aegisx chat
AEGISX_ALLOWED_TOOLS=execute_code,run_tests aegisx schedule run   # izin eksplisit
```

Flag berlaku untuk `chat`, `plan`, dan `schedule run`. Di mode `ask`, prompt
persetujuan menawarkan **always allow**, yang menambahkan tool ke allow-list
per sesi tanpa menyentuh konfigurasi tersimpan.

### Mengelola izin dari chat

```
/permissions                                  # kebijakan, tool yang digate, 5 keputusan audit terakhir
/permissions mode <allow-all|ask|read-only>   # ganti mode (tersimpan ke config)
/permissions allow <tool>                     # tidak pernah tanya untuk tool ini lagi
/permissions deny <tool>                      # blokir — bahkan tool aman, bahkan di allow-all
/permissions reset <tool>                     # hapus override allow/deny-nya
```

Sesi contoh (keluaran nyata):

```
You: /permissions mode read-only
✅ Permission mode: read-only

You: /permissions deny execute_code
✅ deny: execute_code

You: /permissions
         🔐 Tool Permissions
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Setting             ┃ Value        ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ Mode                │ read-only    │
│ Runtime             │ interactive  │
│ Allowed (no prompt) │ —            │
│ Denied              │ execute_code │
└─────────────────────┴──────────────┘
Audit log: /tmp/aegisx-demo/audit.log
Gated tools: execute_code (dangerous), code_edit (caution), run_tests (caution)
Usage: /permissions | /permissions mode <mode> | /permissions allow|deny|reset <tool>
```

`/permissions allow tool_ngasal` ditolak dengan daftar nama tool yang valid,
dan lokasi log audit dicetak bersama kebijakannya agar keputusan bisa diperiksa
belakangan.

**Eksekusi tanpa pengawasan gagal-tertutup.** `aegisx schedule run` tidak punya
orang untuk menjawab prompt, jadi di mode `ask` tool berbahaya *ditolak*
daripada disetujui diam-diam. Beri izin eksplisit lewat `AEGISX_ALLOWED_TOOLS`
atau `--permission-mode allow-all`.

Setiap keputusan ditambahkan ke `~/.aegisx/audit.log` sebagai JSONL — tool,
risiko, putusan, siapa yang memutuskan — dengan argumen berbentuk kredensial
disensor.

## 📊 Observabilitas

Setiap pemanggilan LLM mencatat satu baris JSONL di `~/.aegisx/usage.jsonl`:
jumlah token, provider, model, dan run id. Karena tracker membungkus provider
LLM itu sendiri, **semua** jalur — chat, eksekusi plan, run jadwal dan daemon
— terukur. Periksa pemakaian tanpa keluar dari terminal:

```bash
aegisx usage                    # Ringkasan token/biaya lintas run terakhir
aegisx usage --today
aegisx usage --delegations      # Hanya pekerjaan subagen, satu baris per delegasi
aegisx usage --delegations --run <id>   # Delegasi satu run: langkah dan durasi
aegisx audit --denied           # Hanya penolakan dari gerbang izin
```

**Biaya subagen, dirinci per delegasi.** Pemanggilan di dalam delegasi diberi
tanda id, kedalaman, dan task delegasinya, sehingga biaya subagen tidak lagi
tercampur di total giliran. Tiap delegasi juga menulis satu baris ringkasan
penutup yang mencatat apa yang *dikerjakannya* — langkah, tool call, waktu
dinding, dan bagaimana ia berakhir — dari situlah kolom `Steps`, `Duration`,
dan `Status` berasal:

```text
$ aegisx usage
    📈 Token Usage  (7d)
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Metric          ┃  Value ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ Runs            │      1 │
│ LLM calls       │     11 │
│ Input tokens    │ 17,000 │
│ Output tokens   │  1,400 │
│ Total tokens    │ 18,400 │
│ Parent tokens   │  6,100 │
│ Subagent tokens │ 12,300 │
│ Subagent calls  │     10 │
│ Subagent share  │  66.8% │
└─────────────────┴────────┘
                  🤖 Per delegation (subagent cost)
┏━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┓
┃ Delegation ┃ Depth ┃ Task                         ┃ Steps ┃ Calls ┃ Tokens ┃ Duration ┃ Status    ┃
┡━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━┩
│ def67890   │     2 │ count the errors per service │     3 │     6 │  8,100 │   2m 05s │ budget    │
│ abc12345   │     1 │ summarise the logs           │     2 │     4 │  4,200 │     4.5s │ completed │
└────────────┴───────┴──────────────────────────────┴───────┴───────┴────────┴──────────┴───────────┘
```

`Status` memisahkan delegasi yang selesai (`completed`) dari yang kehabisan
langkah (`budget`) atau waktu (`timeout`) — delegasi yang terpotong bukan
pekerjaan selesai, dan tabel tidak berpura-pura sebaliknya. Tambahkan
`--run <id>` untuk membatasi tampilan ke satu run. Baris yang ditulis sebelum
baris ringkasan ada akan menampilkan `—` untuk langkah dan durasi, bukan nol
yang menyesatkan.

Atribusi bersifat per *task*, bukan per proses: label delegasi dibawa oleh
task asyncio anak, sehingga delegasi yang berjalan bersamaan atau bersarang
tidak saling menagih (cucu menjadi barisnya sendiri). `--json` memuat array
`delegations` yang sama untuk skrip.

## 🧭 Daemon Persisten

Tugas `aegisx schedule` hidup di proses chat; tugas `aegisx daemon` hidup di
SQLite (`~/.aegisx/daemon.db`) dan bertahan melewati restart — daemon yang
jalankan belakangan tetap menjemput semua yang sudah terjadwal, termasuk
mengejar fire yang terlewat saat downtime (dengan batas). Sintaks cron
standar didukung penuh: rentang (`9-17`), step (`*/10`), nama bulan dan hari
(`JAN`, `MON`), dengan aturan OR day-of-month/day-of-week standar.

```bash
aegisx daemon add nightly "ringkas inbox saya" --cron "0 9 * * 1-5"
aegisx daemon add pulse "cek status CI" --every 15m
aegisx daemon run
aegisx daemon list
```

## 📚 RAG (Ingesti Dokumen)

```bash
# Unggah satu file
aegisx ingest ./dokumen.pdf

# Unggah sebuah direktori
aegisx ingest ./docs/

# Cari basis pengetahuan
aegisx search "apa itu rate limit API?"
```

Format didukung: `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`, `.yaml`, `.toml`, `.pdf`

Memerlukan ChromaDB (`pip install chromadb`) — selain itu semua yang agen
butuhkan sudah terpasang secara bawaan.

## 🧪 Test

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m pytest --cov=aegisx_agent   # gerbang coverage dipaksa
.venv/bin/python -m mypy aegisx_agent           # typecheck ketat (gerbang yang sama dengan CI)
```

Suite-nya mencakup loop agentic (pencocokan id panggilan tool, penahanan
kegagalan, batch paralel), registri tool, gerbang izin beserta jalur
fail-closed-nya, log audit dan redaksinya, scheduler, pustaka skill, konversi
wire-format provider, dan ujung-ke-ujung terhadap server HTTP tiruan yang
kompatibel OpenAI/Anthropic.

## 🪝 Git hooks

Hook keamanan (filter pesan commit, penjaga rahasia saat pre-push) berada di
`.githooks/` dan diaktifkan per clone dengan:

```bash
git config core.hooksPath .githooks
```

## ⚙️ Konfigurasi

Semua pengaturan bisa dikonfigurasi lewat variabel lingkungan (awalan `AEGISX_`):

```env
# file .env
AEGISX_LLM_PROVIDER=custom
AEGISX_CUSTOM_BASE_URL=https://api.together.xyz/v1
AEGISX_CUSTOM_API_KEY=kunci-anda
AEGISX_CUSTOM_MODEL=meta-llama/Llama-3-70b-chat-hf
AEGISX_TEMPERATURE=0.7
AEGISX_MAX_ITERATIONS=15
AEGISX_MEMORY_ENABLED=true
AEGISX_RAG_ENABLED=true
AEGISX_SHELL_ENABLED=false
AEGISX_PROJECT_CONTEXT_ENABLED=true    # cwd, stack, git, AGENTS.md di prompt

# Izin
AEGISX_PERMISSION_MODE=ask           # allow-all | ask | read-only
AEGISX_ALLOWED_TOOLS=                # mis. execute_code,run_tests
AEGISX_DENIED_TOOLS=                 # mis. shell
AEGISX_AUDIT_LOG_ENABLED=true
```

## 🧩 Plugin dan tugas autonomous

Plugin tool terversi hanya dimuat secara eksplisit dari module atau path Python;
plugin tidak dijalankan hanya karena AegisX diimpor. Semua plugin tetap melewati
registri tool dan gerbang izin. Tugas scheduler menyimpan checkpoint, melanjutkan
eksekusi setelah restart, mendukung pembatalan/resume, exponential backoff, dan
pause otomatis setelah kegagalan identik berulang.

### Plugin bawaan

Tiga plugin opsional disertakan dalam paket dan dimuat dengan cara eksplisit
yang sama — mengimpor AegisX tidak pernah mengaktifkannya:

```python
agent.load_plugin_module("aegisx_agent.plugins.builtin.browser")   # read_page, http_get
agent.load_plugin_module("aegisx_agent.plugins.builtin.github")    # gh_api, list_issues, get_file
agent.load_plugin_module("aegisx_agent.plugins.builtin.database")  # sql_query (SELECT saja)
agent.load_plugin_module("aegisx_agent.plugins.builtin.all")       # semuanya di atas
```

- **browser** — ambil halaman web sebagai teks polos, atau panggil API JSON.
  Permintaan ke host private/loopback ditolak kecuali
  `AEGISX_ALLOW_PRIVATE_HTTP=1` (penjaga SSRF); risiko `caution`.
- **github** — panggil GitHub REST API (repo, issue, file) dengan token opsional
dari `AEGISX_GITHUB_TOKEN`; risiko `caution`.
- **database** — jalankan kueri **read-only** `SELECT`/`WITH` ke file SQLite
  (`AEGISX_DB_PATH` atau per panggilan), dengan batas baris dan daftar kata
  yang dilarang; risiko `caution`.

Masing-masing tetap melewati gerbang izin: di mode `read-only` semua panggilan
jaringan/database ditolak; di mode `ask` operator menyetujuinya.

Lihat [dokumentasi plugin dan tugas autonomous](docs/plugins-and-autonomous-tasks.md)
untuk API Python dan detail lifecycle. CLI-nya mengikuti: `aegisx plugin
list|load|unload` menampilkan setiap plugin beserta risiko dan putusan gate di
current mode, sedangkan `aegisx schedule cancel|resume|checkpoint` mengontrol
tugas tanpa keluar dari terminal.

### Server MCP (Model Context Protocol)

AegisX dapat mengonsumsi tool dari **server MCP apa pun** — lewat pipeline
versi dan gerbang izin yang sama, sehingga tool eksternal tidak pernah menjadi
pintu tikus:

```bash
pip install "aegisx-agent[mcp]"      # ekstra opsional: SDK mcp resmi
```

```bash
aegisx mcp add files npx -y @modelcontextprotocol/server-filesystem /tmp
aegisx mcp connect files             # → tool plugin_mcp_files_* terdaftar
aegisx mcp list                      # status + tool per server
aegisx mcp disconnect files
```

- Tool tiba sebagai `plugin_mcp_<server>_<tool>` dan muncul di `/tools` dan
  `/plugin list` lengkap dengan putusan gate untuk mode saat ini.
- Kode eksternal bawaannya berisiko `caution`; tool yang deskripsinya
  menyebut *dangerous*/*destructive*/*irreversible* naik ke `dangerous`. Di
  mode `read-only`, tool MCP ditolak.
- Konfigurasi di `~/.aegisx/mcp_servers.json` dengan bentuk `mcpServers` yang
  sama seperti Claude Desktop; override risiko per tool lewat `tool_risks`.

Lihat [panduan integrasi MCP](docs/mcp-integration.md) untuk model keamanan,
referensi konfigurasi, dan catatan lifecycle.

### Delegasi subagen

Agen dapat memecah pekerjaan dengan menjalankan **subagen** — agen bersarang
dengan riwayat pesan sendiri, kumpulan tool terbatas, dan anggaran langkah
yang keras:

- Defaultnya adalah builtin read-only (`calculator`, `datetime`); model dapat
  meminta lebih lewat argumen opsional `tools`, tetapi nama yang tidak dikenal
  disaring dan **`spawn_subagent` sendiri tidak pernah diberikan secara
  eksplisit**.
- Setiap pemanggilan tool anak melewati **gerbang izin yang sama** dengan
  induknya — delegasi bukan jalan tikus. Pada mode `read-only`, percobaan
  tulisan anak ditolak seperti induknya.
- Anggaran langkah bersifat struktural (`max_iterations` loop anak), bukan
  sekadar saran di prompt; anak yang berhenti karena anggaran mengatakannya
  di laporannya.
- Nesting dibatasi kedalaman (`AEGISX_SUBAGENT_MAX_DEPTH`, default 2): anak di
  bawah batas mendapat `spawn_subagent` sendiri yang terikat pada kumpulan
  tool yang lebih sempit, sehingga kumpulan tool hanya menyusut turun
  generasi.

```env
AEGISX_SUBAGENT_MAX_STEPS=8      # iterasi per eksekusi anak
AEGISX_SUBAGENT_MAX_DEPTH=2      # generasi nesting
AEGISX_SUBAGENT_TIMEOUT=120      # detik per eksekusi anak
AEGISX_SUBAGENT_ENABLED=true     # set false untuk menghapus tool ini
AEGISX_SUBAGENT_PROGRESS=steps   # quiet | steps | verbose
```

Token anak dicatat oleh usage tracker dengan run id induk, tetapi ditandai
id, kedalaman, dan task delegasinya sendiri, sehingga `aegisx usage`
melaporkan biaya penuh tugas yang didelegasikan *dan* rinciannya per delegasi
(`aegisx usage --delegations`).

**Telemetri langsung.** Saat streaming (`aegisx chat`), delegasi mengumumkan
dirinya, melaporkan setiap pemanggilan tool anak, lalu ditutup dengan **cara ia
berakhir dan berapa biayanya** — sehingga delegasi yang sudah selesai tidak
lagi terlihat seperti yang masih berjalan:

```text
⏵ subagent (depth 1, budget 8): compute 2+2
  ⏳ subagent step: calculator ✅
✓ subagent (depth 1) completed: 2 steps, 1 tool call, 10 tokens, 0.1s
🔧 spawn_subagent: ✅
```

Baris hasil membawa salah satu dari tiga status, ditandai agar mudah dibaca:

| Status | Arti |
|--------|------|
| `✓ completed` | Anak menyelesaikan tugasnya dalam batas. |
| `⚠ hit its step budget` | Anak kehabisan langkah; laporannya menyatakan tugas mungkin belum tuntas. |
| `⏹ timed out` | Batas waktu membatalkan run (biaya tidak dilaporkan: loop yang dibatalkan tidak punya trace). |

Seberapa banyak yang dicetak diatur oleh `AEGISX_SUBAGENT_PROGRESS`:

| Level | Yang masuk ke stream |
|-------|----------------------|
| `quiet` | Tidak ada — delegasi tidak terlihat sampai selesai. |
| `steps` (default) | Baris mulai, satu baris per pemanggilan tool anak, dan baris hasil (status + biaya). |
| `verbose` | Semua di atas, plus id delegasi pada baris hasil, untuk dicocokkan dengan `aegisx usage --delegations`. |

Delegasi bersarang melaporkan event bertanda kedalaman ke stream yang sama dan
mewarisi level induknya; tiap level juga berlaku untuk kegagalan dan timeout,
jadi `quiet` tetap senyap meskipun anak gagal.

Baris yang sama juga muncul saat tidak ada stream yang membawanya: `aegisx run`
dan `aegisx chat --no-stream` mencetaknya saat kejadian (redup, agar jawaban
tetap menjadi fokus) alih-alih membiarkan tugas yang didelegasikan senyap
sampai selesai.

## 🏗️ Arsitektur

```
aegisx_agent/                 # paket flat di root repo (tanpa src/)
├── core/                     # Runtime agen
│   ├── agent.py              # Orkestrator AegisXAgent dan chat/perencanaan
│   ├── rag_api.py            # API ingestion dan pencarian RAG
│   ├── memory_api.py         # API fakta, percakapan, sesi, dan preferensi
│   ├── scheduler_api.py      # API tugas terjadwal dan daemon scheduler
│   ├── loop.py               # Loop tool-use agentic (streaming + retry)
│   └── config.py             # Konfigurasi (pydantic-settings)
├── cli/                      # CLI terminal kaya
│   ├── app.py                # Typer app + konsol Rich bersama (satu sumber)
│   ├── main.py               # Perpipaan config + entry point typer
│   ├── interactive.py        # Loop chat, progress animasi, dispatch slash
│   ├── commands/             # Handler slash: permissions, code, schedule
│   └── __init__.py           # Re-export untuk pemakaian programatik
├── plugins/                  # Manifest tool terversi dan loader eksplisit
├── security/                 # Gerbang izin + log audit
├── llm/                      # Dukungan LLM multi-provider
│   ├── base.py               # Abstraksi dasar
│   ├── factory.py            # Factory provider
│   ├── openai_provider.py
│   ├── anthropic_provider.py
│   ├── ollama_provider.py
│   ├── groq_provider.py
│   └── custom_provider.py    # Endpoint apa pun yang kompatibel OpenAI
├── tools/                    # Sistem tool
│   ├── base.py               # Abstraksi tool
│   ├── registry.py           # Registri tool
│   ├── coding/               # Git, editor, test runner, pencarian kode
│   ├── web_search.py
│   ├── web_scraper.py
│   ├── code_executor.py
│   ├── file_ops.py
│   ├── shell.py
│   ├── calculator.py
│   ├── db_query.py
│   ├── api_caller.py
│   ├── datetime_tool.py
│   └── rag_search.py
├── memory/                   # Sistem memori
│   ├── store.py              # Memori percakapan + jangka panjang
│   └── advanced.py           # Prompt memory, sesi, model pengguna
├── rag/                      # Sistem RAG
│   └── engine.py             # Vector store ChromaDB
├── scheduler/                # Mesin tugas autonomous persisten
│   ├── task.py               # Model task, checkpoint, retry/cancel state
│   └── engine.py             # Resume, backoff, deteksi loop, daemon
├── skills/                   # Penangkapan + manajemen skill
├── planning/                 # Sistem perencanaan
│   └── react.py              # Loop penalaran ReAct
├── personas/                 # Sistem persona
│   └── loader.py             # Loader persona kustom
├── project.py                # Deteksi konteks proyek
├── config.py                 # Re-export back-compat dari core.config
└── agent_loop.py             # Re-export back-compat dari core.loop
```

## 📚 Dokumentasi

- [Plugin dan tugas autonomous](docs/plugins-and-autonomous-tasks.md) — tool terversi, checkpoint, pembatalan, retry, dan deteksi loop
- [Plugin dan tugas autonomous](docs/plugins-and-autonomous-tasks.md) — tool terversi, checkpoint, pembatalan, retry, dan deteksi loop
- [Arsitektur core](docs/core.md) — batas modul runtime agen dan API RAG, memori, serta scheduler
- [Arsitektur CLI](docs/cli.md) — cara paket CLI dipecah dan aturannya
- [Versi bahasa Inggris](README.md) — README resmi

## 📜 Changelog

Lihat [CHANGELOG.md](CHANGELOG.md) untuk perubahan penting dan perbaikan bug.

## 📄 Lisensi

MIT
