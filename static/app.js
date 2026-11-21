(() => {
  'use strict';

  const PIN_STORAGE_KEY = 'g2k_app_pin';
  const AUTHOR_STORAGE_KEY = 'g2k_default_author';

  const els = {
    form: document.getElementById('converterForm'),
    titleInput: document.getElementById('titleInput'),
    markdownInput: document.getElementById('markdownInput'),
    pinInput: document.getElementById('pinInput'),
    authorInput: document.getElementById('authorInput'),
    togglePinBtn: document.getElementById('togglePinBtn'),
    forgetPinBtn: document.getElementById('forgetPinBtn'),
    pasteBtn: document.getElementById('pasteBtn'),
    sampleBtn: document.getElementById('sampleBtn'),
    clearBtn: document.getElementById('clearBtn'),
    contentStats: document.getElementById('contentStats'),
    downloadBtn: document.getElementById('downloadBtn'),
    sendKindleBtn: document.getElementById('sendKindleBtn'),
    statusBanner: document.getElementById('statusBanner'),
    statusSpinner: document.getElementById('statusSpinner'),
    statusTitle: document.getElementById('statusTitle'),
    statusDetail: document.getElementById('statusDetail'),
    serverBadge: document.getElementById('serverBadge'),
    installBtn: document.getElementById('installBtn'),
  };

  let isBusy = false;
  let deferredInstallPrompt = null;

  const SAMPLE_MARKDOWN = `# Hukum Pertama Termodinamika

Hukum pertama menyatakan kekekalan energi untuk sistem tertutup:

$$\\Delta U = Q - W$$

dengan $\\Delta U$ adalah perubahan energi dalam, $Q$ adalah kalor yang masuk ke sistem, dan $W$ adalah kerja yang dilakukan sistem.

## Bentuk Diferensial

Untuk proses reversibel gas ideal, kita tulis:

\\[
dU = \\delta Q - P\\,dV = n C_v\\,dT
\\]

Reaksi pembakaran sederhana: $\\ce{CH4 + 2 O2 -> CO2 + 2 H2O}$.

| Besaran | Simbol | Satuan SI |
| --- | --- | --- |
| Energi dalam | $U$ | $\\text{J}$ |
| Kalor | $Q$ | $\\text{J}$ |
| Kerja | $W$ | $\\text{J}$ |

\`\`\`python
# Contoh perhitungan cepat (variabel $HOME tidak rusak)
def delta_u(q_joule: float, w_joule: float) -> float:
    return q_joule - w_joule
\`\`\`
`;

  function showStatus(type, title, detail = '') {
    els.statusBanner.classList.remove('hidden', 'success', 'error', 'loading');
    els.statusSpinner.classList.add('hidden');

    if (type === 'loading') {
      els.statusBanner.classList.add('loading');
      els.statusSpinner.classList.remove('hidden');
    } else if (type === 'success') {
      els.statusBanner.classList.add('success');
    } else if (type === 'error') {
      els.statusBanner.classList.add('error');
    }

    els.statusTitle.textContent = title;
    els.statusDetail.textContent = detail;
  }

  function hideStatus() {
    els.statusBanner.classList.add('hidden');
  }

  function setBusy(busy) {
    isBusy = busy;
    els.downloadBtn.disabled = busy;
    els.sendKindleBtn.disabled = busy;
    els.pasteBtn.disabled = busy;
    els.sampleBtn.disabled = busy;
    els.clearBtn.disabled = busy;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }

  function countApproxFormulas(md) {
    if (!md) return 0;
    // Strip fenced code blocks and inline code roughly for estimation
    const noCode = md
      .replace(/```[\s\S]*?```/g, '')
      .replace(/~~~[\s\S]*?~~~/g, '')
      .replace(/`[^`\n]+`/g, '');
    const blocks = (noCode.match(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]/g) || []).length;
    const stripped = noCode.replace(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]/g, '');
    const inlineParen = (stripped.match(/\\\([\s\S]+?\\\)/g) || []).length;
    const inlineDollar = (stripped.match(/(?<!\\)\$(?!\s|\$)(?:\\\$|[^$\n])+?(?<!\s)\$(?!\d|\$)/g) || []).length;
    return blocks + inlineParen + inlineDollar;
  }

  function updateStats() {
    const text = els.markdownInput.value || '';
    const chars = text.length.toLocaleString('id-ID');
    const mathCount = countApproxFormulas(text);
    els.contentStats.textContent = `${chars} karakter • ${mathCount} rumus terdeteksi`;
  }

  function maybeAutoFillTitleFromMarkdown() {
    if (els.titleInput.value.trim()) return;
    const md = els.markdownInput.value || '';
    const match = md.match(/^\s*#\s+(.+?)\s*$/m);
    if (match && match[1]) {
      els.titleInput.value = match[1].replace(/[*_`~]/g, '').trim().slice(0, 120);
    }
  }

  function syncForgetPinVisibility() {
    const hasSaved = Boolean(localStorage.getItem(PIN_STORAGE_KEY));
    els.forgetPinBtn.classList.toggle('hidden', !hasSaved);
  }

  function loadSavedPrefs() {
    try {
      const savedPin = localStorage.getItem(PIN_STORAGE_KEY);
      if (savedPin) {
        els.pinInput.value = savedPin;
      }
      const savedAuthor = localStorage.getItem(AUTHOR_STORAGE_KEY);
      if (savedAuthor) {
        els.authorInput.value = savedAuthor;
      }
      syncForgetPinVisibility();
    } catch (_err) {
      // Ignore storage errors in restricted browsing modes
    }
  }

  function persistPin(pin) {
    try {
      if (pin) {
        localStorage.setItem(PIN_STORAGE_KEY, pin);
      }
      syncForgetPinVisibility();
    } catch (_err) {
      // Ignore
    }
  }

  function validateInputs() {
    maybeAutoFillTitleFromMarkdown();

    const title = els.titleInput.value.trim();
    const markdown = els.markdownInput.value.trim();
    const pin = els.pinInput.value.trim();
    const author = els.authorInput.value.trim();

    if (!title) {
      showStatus('error', 'Judul dokumen wajib diisi.', 'Masukkan judul atau mulai Markdown dengan heading # Judul.');
      els.titleInput.focus();
      return null;
    }
    if (!markdown) {
      showStatus('error', 'Konten Markdown masih kosong.', 'Tempel hasil tombol Copy dari Gemini terlebih dahulu.');
      els.markdownInput.focus();
      return null;
    }
    if (!pin) {
      showStatus('error', 'PIN keamanan belum diisi.', 'Masukkan APP_PIN yang sesuai dengan konfigurasi server.');
      els.pinInput.focus();
      return null;
    }

    persistPin(pin);
    try {
      if (author) {
        localStorage.setItem(AUTHOR_STORAGE_KEY, author);
      } else {
        localStorage.removeItem(AUTHOR_STORAGE_KEY);
      }
    } catch (_err) {
      // Ignore
    }

    return {
      pin,
      payload: {
        title,
        markdown: els.markdownInput.value,
        ...(author ? { author } : {}),
      },
    };
  }

  async function extractErrorMessage(response) {
    try {
      const data = await response.json();
      if (data && typeof data.detail === 'string') return data.detail;
      if (data && Array.isArray(data.detail) && data.detail[0]?.msg) {
        return data.detail[0].msg;
      }
    } catch (_err) {
      // Not JSON
    }
    return `Permintaan gagal (HTTP ${response.status}).`;
  }

  function parseFilenameFromResponse(response, fallbackTitle) {
    const xFilename = response.headers.get('X-Filename');
    if (xFilename) {
      try {
        return decodeURIComponent(xFilename);
      } catch (_err) {
        // Fallback below
      }
    }
    const cd = response.headers.get('Content-Disposition') || '';
    const utf8Match = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match && utf8Match[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch (_err) {
        // Fallback below
      }
    }
    const asciiMatch = cd.match(/filename="([^"]+)"/i);
    if (asciiMatch && asciiMatch[1]) {
      return asciiMatch[1];
    }
    const safe = fallbackTitle.replace(/[<>:"/\\|?*]+/g, '').trim() || 'gemini-document';
    return `${safe}.epub`;
  }

  async function handleDownloadEpub() {
    if (isBusy) return;
    const validated = validateInputs();
    if (!validated) return;

    setBusy(true);
    showStatus(
      'loading',
      'Merender rumus LaTeX & menyusun EPUB...',
      'Proses ini memerlukan beberapa detik jika terdapat banyak rumus.'
    );

    try {
      const response = await fetch('/api/generate-epub', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-App-Pin': validated.pin,
        },
        body: JSON.stringify(validated.payload),
      });

      if (!response.ok) {
        const errMsg = await extractErrorMessage(response);
        showStatus('error', 'Gagal membuat EPUB', errMsg);
        return;
      }

      const blob = await response.blob();
      const filename = parseFilenameFromResponse(response, validated.payload.title);
      const mathTotal = response.headers.get('X-Math-Count') || '0';
      const mathEngine = response.headers.get('X-Math-Engine') || '-';

      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

      showStatus(
        'success',
        'EPUB berhasil diunduh!',
        `${filename} (${formatBytes(blob.size)}) • ${mathTotal} rumus dirender via ${mathEngine}.`
      );
    } catch (err) {
      showStatus('error', 'Koneksi terputus', `Tidak dapat menghubungi server: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleSendKindle() {
    if (isBusy) return;
    const validated = validateInputs();
    if (!validated) return;

    setBusy(true);
    showStatus(
      'loading',
      'Merender rumus LaTeX & mengirim ke Kindle...',
      'Menyusun EPUB 3 lalu mengirimkannya ke email Kindle Anda.'
    );

    try {
      const response = await fetch('/api/send-kindle', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-App-Pin': validated.pin,
        },
        body: JSON.stringify(validated.payload),
      });

      if (!response.ok) {
        const errMsg = await extractErrorMessage(response);
        showStatus('error', 'Gagal mengirim ke Kindle', errMsg);
        return;
      }

      const data = await response.json();
      const modeLabel = data.mail_mode === 'apps_script' ? 'Apps Script' : 'SMTP';
      showStatus(
        'success',
        'Berhasil dikirim ke Kindle!',
        `${data.filename} (${formatBytes(data.size_bytes)}) • ${data.math_total} rumus (${data.math_engine}) • Mode ${modeLabel}.`
      );
    } catch (err) {
      showStatus('error', 'Koneksi terputus', `Tidak dapat menghubungi server: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function checkServerHealth() {
    try {
      const resp = await fetch('/api/health', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const info = await resp.json();

      if (!info.pin_configured) {
        els.serverBadge.textContent = 'APP_PIN belum diset';
        els.serverBadge.className = 'status-pill warn';
        return;
      }

      const engineTag = info.math_engine === 'mathjax' ? 'MathJax' : 'Matplotlib';
      const mailTag = info.kindle_configured
        ? info.mail_mode === 'apps_script'
          ? 'GAS'
          : 'SMTP'
        : 'EPUB Only';

      els.serverBadge.textContent = `${engineTag} • ${mailTag}`;
      els.serverBadge.className = 'status-pill ready';
    } catch (_err) {
      els.serverBadge.textContent = 'Offline';
      els.serverBadge.className = 'status-pill warn';
    }
  }

  function registerServiceWorker() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        // Ignore SW errors in unsupported environments
      });
    }
  }

  // Event bindings
  els.downloadBtn.addEventListener('click', handleDownloadEpub);
  els.sendKindleBtn.addEventListener('click', handleSendKindle);

  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    handleDownloadEpub();
  });

  els.markdownInput.addEventListener('input', () => {
    updateStats();
  });

  els.markdownInput.addEventListener('paste', () => {
    setTimeout(() => {
      maybeAutoFillTitleFromMarkdown();
      updateStats();
    }, 10);
  });

  els.pinInput.addEventListener('change', () => {
    const val = els.pinInput.value.trim();
    if (val) persistPin(val);
  });

  els.togglePinBtn.addEventListener('click', () => {
    const isPassword = els.pinInput.type === 'password';
    els.pinInput.type = isPassword ? 'text' : 'password';
    els.togglePinBtn.textContent = isPassword ? 'Sembunyi' : 'Lihat';
  });

  els.forgetPinBtn.addEventListener('click', () => {
    try {
      localStorage.removeItem(PIN_STORAGE_KEY);
    } catch (_err) {
      // Ignore
    }
    els.pinInput.value = '';
    syncForgetPinVisibility();
    showStatus('info', 'PIN tersimpan dihapus.', 'Masukkan kembali PIN saat ingin mengonversi dokumen.');
  });

  els.pasteBtn.addEventListener('click', async () => {
    try {
      if (!navigator.clipboard || !navigator.clipboard.readText) {
        showStatus('error', 'Clipboard tidak tersedia.', 'Tempel secara manual (tekan lama / Ctrl+V) di kotak teks.');
        return;
      }
      const clipText = await navigator.clipboard.readText();
      if (!clipText) {
        showStatus('error', 'Clipboard kosong.', 'Salin dulu jawaban dari tombol Copy di Gemini.');
        return;
      }
      els.markdownInput.value = clipText;
      maybeAutoFillTitleFromMarkdown();
      updateStats();
      hideStatus();
    } catch (_err) {
      showStatus(
        'error',
        'Izin clipboard ditolak browser.',
        'Silakan tempel manual (tekan lama atau Ctrl+V) ke dalam kotak teks.'
      );
    }
  });

  els.sampleBtn.addEventListener('click', () => {
    els.titleInput.value = 'Hukum Pertama Termodinamika';
    els.markdownInput.value = SAMPLE_MARKDOWN;
    updateStats();
    hideStatus();
  });

  els.clearBtn.addEventListener('click', () => {
    els.markdownInput.value = '';
    updateStats();
    hideStatus();
    els.markdownInput.focus();
  });

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    els.installBtn.classList.remove('hidden');
  });

  els.installBtn.addEventListener('click', async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    try {
      await deferredInstallPrompt.userChoice;
    } finally {
      deferredInstallPrompt = null;
      els.installBtn.classList.add('hidden');
    }
  });

  // Init
  loadSavedPrefs();
  updateStats();
  checkServerHealth();
  registerServiceWorker();
})();
