# Gemini2Kindle Web

Aplikasi web pribadi lintas perangkat (HP & Desktop) berbasis **Python FastAPI + PWA ringan** untuk mengubah teks Markdown + rumus LaTeX hasil tombol **Copy** dari Gemini menjadi file **EPUB 3 ramah e-ink** dan langsung mengunduh atau mengirimkannya ke Kindle Anda.

## Fitur Utama

- **PWA Mobile-First**: Ringan, minimalis, mendukung *Add to Home Screen* di Android Chrome & iOS Safari, menyimpan PIN keamanan di `localStorage` browser.
- **Ekstraksi LaTeX Aman**: Memisahkan rumus blok (`$$...$$`, `\[...\]`) dan sebaris (`$...$`, `\(...\)`) sebelum parsing Markdown sehingga karakter `_` dan `\` tidak rusak, sekaligus melindungi blok kode (`$HOME`, dll.) serta simbol mata uang (`$5 dan $10`).
- **Render LaTeX 300 DPI Transparan**:
  - **Mode Utama (`mathjax`)**: Batch renderer Node.js lokal (`mathjax-full` + `@resvg/resvg-js`) dengan dukungan paket TeX luas (AMS, matriks, `\ce{}` kimia) dan metrik *baseline* presisi untuk rumus sebaris.
  - **Mode Ringan / Fallback (`matplotlib`)**: Fallback otomatis pure-Python (`matplotlib.mathtext`) atau aktifkan langsung lewat `MATH_ENGINE=matplotlib`.
- **EPUB 3 Ramah E-Ink**: Font serif, `line-height: 1.5`, blok kode berborder kiri tipis tanpa latar gelap, tabel bersih, rumus blok rata tengah (`max-width: 100%`), rumus sebaris sejajar vertikal dengan teks, serta TOC otomatis.
- **2 Mode Pengiriman Email**:
  - `smtp`: Gmail SMTP (SSL port `465` atau STARTTLS port `587`) dengan Gmail App Password.
  - `apps_script`: Relay via HTTP POST ke Google Apps Script Web App jika hosting gratis memblokir port SMTP.
- **Keamanan PIN**: Seluruh endpoint `POST` (`/api/generate-epub`, `/api/send-kindle`) dilindungi header `X-App-Pin` yang dicocokkan dengan `APP_PIN`.

---

## Persiapan Kindle & Email Pengirim

1. **Daftarkan email pengirim ke Amazon Kindle**:
   - Buka Amazon → **Manage Your Content and Devices** → **Preferences** → **Personal Document Settings**.
   - Catat alamat `@kindle.com` Anda untuk `KINDLE_EMAIL`.
   - Tambahkan alamat Gmail pengirim Anda ke **Approved Personal Document E-mail List**.
2. **Pilih salah satu metode kirim**:
   - **Opsi A — Gmail SMTP (`MAIL_MODE=smtp`)**:
     - Aktifkan 2-Step Verification di akun Google Anda, buat **App Password** (16 digit), lalu masukkan ke `SMTP_USER` dan `SMTP_PASSWORD`.
   - **Opsi B — Google Apps Script (`MAIL_MODE=apps_script`, direkomendasikan untuk hosting gratis)**:
     - Buka [script.google.com](https://script.google.com) → **New Project**.
     - Salin isi [`scripts/google_apps_script.gs`](scripts/google_apps_script.gs) ke `Code.gs`.
     - Ganti `FALLBACK_SECRET_TOKEN` (atau simpan di *Project Settings → Script Properties* dengan key `APPS_SCRIPT_TOKEN`).
     - Klik **Deploy → New deployment → Web app**:
       - *Execute as*: **Me**
       - *Who has access*: **Anyone**
     - Salin URL deployment (`.../exec`) ke `APPS_SCRIPT_URL` dan token ke `APPS_SCRIPT_TOKEN`.

---

## Menjalankan Secara Lokal

### Menggunakan Docker (Direkomendasikan)

```bash
cp .env.example .env
# Edit .env dan isi minimal APP_PIN (serta konfigurasi Kindle jika ingin fitur kirim email)

docker build -t gemini2kindle-web .
docker run --rm -p 7860:7860 --env-file .env gemini2kindle-web
```

Buka `http://localhost:7860`.

### Tanpa Docker (Python + Node.js)

```bash
# 1. Dependensi Python
python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. (Opsional untuk mode MathJax) Dependensi Node.js
cd math_renderer && npm install --omit=dev && cd ..

# 3. Konfigurasi & jalankan
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 7860 --reload
```

> Jika Node.js tidak terpasang, aplikasi otomatis menggunakan fallback `matplotlib.mathtext` (atau set `MATH_ENGINE=matplotlib`).

---

## Panduan Deploy Singkat

### 1. Deploy di Render (Docker Web Service)

1. Push proyek ini ke repository GitHub/GitLab pribadi.
2. Di dashboard [Render](https://render.com), klik **New + → Web Service** dan hubungkan repository Anda.
3. Pilih **Language / Runtime: Docker** (Render otomatis mendeteksi `Dockerfile` dan menyuntikkan env var `PORT`).
4. Di tab **Environment**, tambahkan variabel wajib:
   - `APP_PIN`
   - `KINDLE_EMAIL`
   - `MAIL_MODE` (`apps_script` disarankan untuk tier gratis, atau `smtp`)
   - Variabel sesuai mode (`APPS_SCRIPT_URL` + `APPS_SCRIPT_TOKEN` atau `SMTP_USER` + `SMTP_PASSWORD` + `SMTP_PORT`)
5. Klik **Create Web Service**. Health check tersedia di `/api/health`.

### 2. Deploy di Hugging Face Spaces (Docker Space)

1. Buat Space baru di [huggingface.co/spaces](https://huggingface.co/spaces):
   - **Space SDK**: `Docker` (Blank template)
   - **Visibility**: `Private` (disarankan untuk penggunaan pribadi) atau `Public` (tetap terlindungi `APP_PIN`).
2. Jika mengunggah langsung ke repo Space, tambahkan blok frontmatter YAML berikut di baris paling atas `README.md` repo Space Anda:

```yaml
---
title: Gemini2Kindle Web
emoji: 📚
colorFrom: gray
colorTo: stone
sdk: docker
app_port: 7860
---
```

3. Masuk ke tab **Settings → Variables and secrets** di Space, lalu masukkan seluruh rahasia (`APP_PIN`, `KINDLE_EMAIL`, `MAIL_MODE`, dan kredensial `SMTP_*` atau `APPS_SCRIPT_*`) sebagai **Repository Secrets**.
4. Push seluruh kode ke Space. Container otomatis berjalan sebagai non-root user UID `1000` di port `7860`.

---

## Memasang Sebagai Aplikasi di HP (PWA)

- **Android (Chrome)**: Buka URL aplikasi → ketuk tombol **+ Pasang** di pojok kanan atas (atau menu `⋮` → **Add to Home screen / Install app**).
- **iPhone / iPad (Safari)**: Buka URL aplikasi → ketuk ikon **Share** → **Add to Home Screen**.

---

## Menjalankan Pengujian

```bash
pip install pytest
pytest -v
```
