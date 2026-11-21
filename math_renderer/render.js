'use strict';

/**
 * Batch LaTeX -> SVG (MathJax 3) -> Transparent PNG 300 DPI (@resvg/resvg-js) renderer.
 *
 * Reads JSON from stdin:
 * {
 *   "dpi": 300,
 *   "items": [
 *     { "key": "k0", "latex": "E = mc^2", "display": true }
 *   ]
 * }
 *
 * Writes JSON to stdout:
 * {
 *   "results": {
 *     "k0": {
 *       "ok": true,
 *       "png_base64": "...",
 *       "width_px": 240,
 *       "height_px": 72,
 *       "width_em": 4.21,
 *       "height_em": 1.26,
 *       "valign_em": -0.18
 *     }
 *   }
 * }
 */

const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
const { AllPackages } = require('mathjax-full/js/input/tex/AllPackages.js');
const { Resvg } = require('@resvg/resvg-js');

const EX_TO_EM = 0.46;
const PAD_EX = 0.1;
const MAX_PX_WIDTH = 2200;
const MAX_PX_HEIGHT = 2800;

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', (chunk) => chunks.push(chunk));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
  });
}

function parseLengthToEx(rawValue) {
  if (!rawValue) return null;
  const match = String(rawValue).trim().match(/^(-?\d+(?:\.\d+)?)(ex|em|px)?$/i);
  if (!match) return null;
  const num = parseFloat(match[1]);
  const unit = (match[2] || 'ex').toLowerCase();
  if (!Number.isFinite(num)) return null;
  if (unit === 'ex') return num;
  if (unit === 'em') return num / EX_TO_EM;
  if (unit === 'px') return num / 8.0;
  return num;
}

function createMathDocument() {
  const adaptor = liteAdaptor();
  RegisterHTMLHandler(adaptor);

  const safePackages = AllPackages.filter(
    (pkg) => pkg !== 'bussproofs' && pkg !== 'require' && pkg !== 'noerrors'
  );

  const tex = new TeX({
    packages: safePackages,
    formatError(_jax, err) {
      throw err;
    },
  });

  const svg = new SVG({
    fontCache: 'none',
    exFactor: 0.5,
  });

  const html = mathjax.document('', {
    InputJax: tex,
    OutputJax: svg,
  });

  return { adaptor, html };
}

function renderOne(htmlDoc, adaptor, item, dpi) {
  const latex = String(item.latex || '').trim();
  const display = Boolean(item.display);

  if (!latex) {
    return { ok: false, error: 'Empty LaTeX expression' };
  }

  let svgStr;
  try {
    const node = htmlDoc.convert(latex, {
      display,
      em: 16,
      ex: 8,
      containerWidth: 80 * 16,
    });
    const rawHtml = adaptor.innerHTML(node);
    const svgMatch = rawHtml.match(/<svg[\s\S]*<\/svg>/i);
    if (!svgMatch) {
      return { ok: false, error: 'MathJax did not produce an SVG element' };
    }
    svgStr = svgMatch[0];
  } catch (err) {
    return { ok: false, error: err && err.message ? String(err.message) : String(err) };
  }

  if (svgStr.includes('data-mjx-error')) {
    return { ok: false, error: 'MathJax reported a TeX syntax error' };
  }

  const openTagMatch = svgStr.match(/<svg\b[^>]*>/i);
  if (!openTagMatch) {
    return { ok: false, error: 'Malformed SVG root tag' };
  }
  const openTag = openTagMatch[0];

  const viewBoxMatch = openTag.match(/viewBox="([^"]+)"/i);
  if (!viewBoxMatch) {
    return { ok: false, error: 'Missing SVG viewBox' };
  }
  const vbParts = viewBoxMatch[1].trim().split(/\s+/).map(Number);
  if (vbParts.length !== 4 || vbParts.some((v) => !Number.isFinite(v))) {
    return { ok: false, error: 'Invalid SVG viewBox values' };
  }

  const [vx, vy, vw, vh] = vbParts;
  if (vw <= 0 || vh <= 0) {
    return { ok: false, error: 'Degenerate zero-size equation' };
  }

  const widthAttr = (openTag.match(/\bwidth="([^"]+)"/i) || [])[1];
  const heightAttr = (openTag.match(/\bheight="([^"]+)"/i) || [])[1];
  const styleAttr = (openTag.match(/\bstyle="([^"]+)"/i) || [])[1] || '';
  const valignMatch = styleAttr.match(/vertical-align:\s*([^;]+)/i);

  let widthEx = parseLengthToEx(widthAttr);
  let heightEx = parseLengthToEx(heightAttr);
  const valignExOrig = parseLengthToEx(valignMatch ? valignMatch[1] : '0ex') || 0;

  // Fallback to MathJax canonical ~442 SVG units per ex if attributes missing
  if (!widthEx || widthEx <= 0) widthEx = vw / 442.0;
  if (!heightEx || heightEx <= 0) heightEx = vh / 442.0;

  const unitsPerEx = vw / widthEx;
  const padUnits = PAD_EX * unitsPerEx;

  const newVx = vx - padUnits;
  const newVy = vy - padUnits;
  const newVw = vw + 2 * padUnits;
  const newVh = vh + 2 * padUnits;

  const paddedWidthEx = widthEx + 2 * PAD_EX;
  const paddedHeightEx = heightEx + 2 * PAD_EX;
  const paddedValignEx = valignExOrig - PAD_EX;

  // Target crisp 300 DPI rasterization at ~15pt base font (1ex ~= 0.46em ~= 28.75px at 300 DPI)
  const baseFontPt = display ? 15.5 : 14.5;
  const pxPerEm = (dpi * baseFontPt) / 72.0;
  const pxPerEx = pxPerEm * EX_TO_EM;

  const aspect = newVh / newVw;
  let targetWidthPx = Math.max(8, Math.round(paddedWidthEx * pxPerEx));
  let targetHeightPx = Math.max(8, Math.round(targetWidthPx * aspect));

  if (targetWidthPx > MAX_PX_WIDTH) {
    targetWidthPx = MAX_PX_WIDTH;
    targetHeightPx = Math.max(8, Math.round(targetWidthPx * aspect));
  }
  if (targetHeightPx > MAX_PX_HEIGHT) {
    targetHeightPx = MAX_PX_HEIGHT;
    targetWidthPx = Math.max(8, Math.round(targetHeightPx / aspect));
  }

  let patchedOpenTag = openTag
    .replace(/\s+\bstyle="[^"]*"/i, '')
    .replace(/\bwidth="[^"]*"/i, `width="${targetWidthPx}"`)
    .replace(/\bheight="[^"]*"/i, `height="${targetHeightPx}"`)
    .replace(/\bviewBox="[^"]*"/i, `viewBox="${newVx.toFixed(2)} ${newVy.toFixed(2)} ${newVw.toFixed(2)} ${newVh.toFixed(2)}"`);

  if (!/\bwidth="/i.test(patchedOpenTag)) {
    patchedOpenTag = patchedOpenTag.replace('<svg', `<svg width="${targetWidthPx}"`);
  }
  if (!/\bheight="/i.test(patchedOpenTag)) {
    patchedOpenTag = patchedOpenTag.replace('<svg', `<svg height="${targetHeightPx}"`);
  }

  const patchedSvg = (patchedOpenTag + svgStr.slice(openTag.length))
    .replace(/currentColor/g, '#000000');

  try {
    const resvg = new Resvg(patchedSvg, {
      background: 'rgba(0, 0, 0, 0)',
      dpi,
      font: {
        loadSystemFonts: true,
        defaultFontFamily: 'DejaVu Serif',
      },
    });
    const rendered = resvg.render();
    const pngBuffer = rendered.asPng();

    return {
      ok: true,
      png_base64: Buffer.from(pngBuffer).toString('base64'),
      width_px: rendered.width,
      height_px: rendered.height,
      width_em: Number((paddedWidthEx * EX_TO_EM).toFixed(3)),
      height_em: Number((paddedHeightEx * EX_TO_EM).toFixed(3)),
      valign_em: Number((paddedValignEx * EX_TO_EM).toFixed(3)),
    };
  } catch (err) {
    return { ok: false, error: `Resvg failed: ${err && err.message ? err.message : String(err)}` };
  }
}

async function main() {
  try {
    const raw = await readStdin();
    const payload = raw.trim() ? JSON.parse(raw) : { items: [] };
    const dpi = Number(payload.dpi) || 300;
    const items = Array.isArray(payload.items) ? payload.items : [];

    const { adaptor, html } = createMathDocument();
    const results = {};

    for (const item of items) {
      results[item.key] = renderOne(html, adaptor, item, dpi);
    }

    process.stdout.write(JSON.stringify({ results }));
  } catch (err) {
    process.stderr.write(`Fatal renderer error: ${err && err.stack ? err.stack : String(err)}\n`);
    process.exit(1);
  }
}

main();
