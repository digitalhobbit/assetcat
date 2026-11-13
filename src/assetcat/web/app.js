// Asset library browser. Preact + htm, no build step. Talks to `assetcat serve` (server.py).
import { html, render, useState, useEffect, useRef, useMemo, useCallback } from './vendor/preact-htm.js';

// --- helpers ----------------------------------------------------------------------------------

const enc = encodeURIComponent;
const libUrl = (p) => '/lib/' + p.split('/').map(enc).join('/');
const THUMBS = [96, 128, 192, 256, 384, 512];
const thumbSize = (px) => THUMBS.find((s) => s >= px * (window.devicePixelRatio || 1)) || 512;
const thumbUrl = (p, px) => `/thumb?path=${enc(p)}&s=${thumbSize(px)}`;
const coverUrl = (id, px) => `/cover?pack=${enc(id)}&s=${thumbSize(px)}`;
const qs = (o) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '' && v !== false) u.set(k, v === true ? '1' : v);
  return u.toString();
};
const bytes = (n) => {
  n = n || 0;
  for (const u of ['B', 'KB', 'MB', 'GB']) {
    if (n < 1024 || u === 'GB') return u === 'B' ? `${n} B` : `${n.toFixed(1)} ${u}`;
    n /= 1024;
  }
};
const secs = (s) => (s == null ? '' : s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`);
const plural = (n, w) => `${(n ?? 0).toLocaleString()} ${w}${n === 1 ? '' : 's'}`;
const NO_THUMB = new Set(['ai', 'cdr', 'aseprite', 'ase', 'pyxel', 'kra', 'xcf', 'sketch', 'fla', 'swf', 'scml', 'spriter', 'afdesign', 'svgz']);
const GLYPH = { graphics: '▦', '3d': '◆', music: '♫', sfx: '♪', audio: '♪', fonts: 'Aa', engine: '⚙', archived: '▣', mixed: '◩', other: '·' };

async function api(url, body) {
  const r = await fetch(url, body === undefined ? undefined : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) {
    let m = r.statusText;
    try { const d = await r.json(); m = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
    throw new Error(m);
  }
  return r.json();
}

const prefs = (() => {
  try { return JSON.parse(localStorage.getItem('assetcat.prefs') || localStorage.getItem('assetdb.prefs') || '{}'); } catch { return {}; }
})();
function savePref(k, v) {
  prefs[k] = v;
  try { localStorage.setItem('assetcat.prefs', JSON.stringify(prefs)); } catch {}
}

// --- tiny global store ------------------------------------------------------------------------

const state = { meta: null, marks: {}, starMenu: null, toast: null, viewer: null, player: null };
const subs = new Set();
function setState(patch) { Object.assign(state, patch); subs.forEach((f) => f()); }
function useStore() {
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((x) => x + 1); subs.add(f); return () => subs.delete(f); }, []);
  return state;
}
let toastTimer;
function toast(msg, err = false) {
  clearTimeout(toastTimer);
  setState({ toast: { msg, err } });
  toastTimer = setTimeout(() => setState({ toast: null }), err ? 6000 : 2500);
}
const fail = (e) => toast(e.message || String(e), true);

async function loadMeta() { setState({ meta: await api('/api/meta') }); }

// Shortlists: a star is filled when the item is in any shortlist; clicking it opens a menu of
// all shortlists to tick or untick, and a field for a new one.
async function loadMarks() {
  try { setState({ marks: await api('/api/shortlists/marks') }); } catch (e) { fail(e); }
}

function Star({ kind, refv, cls = '' }) {
  const s = useStore();
  const lists = s.marks[`${kind}:${refv}`] || [];
  const open = (e) => {
    e.preventDefault(); e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    setState({ starMenu: { kind, ref: refv, x: r.left, y: r.bottom } });
  };
  return html`<button class="starbtn ${lists.length ? 'on' : ''} ${cls}" onClick=${open}
    title=${lists.length ? `In ${lists.join(', ')}. Click to change.` : 'Add to a shortlist'}>${lists.length ? '★' : '☆'}</button>`;
}

function StarMenu() {
  const s = useStore();
  const [name, setName] = useState('');
  const m = s.starMenu;
  useEffect(() => {
    if (!m) return;
    const f = (e) => e.key === 'Escape' && setState({ starMenu: null });
    addEventListener('keydown', f);
    return () => removeEventListener('keydown', f);
  }, [m]);
  if (!m) return null;
  const inLists = new Set(s.marks[`${m.kind}:${m.ref}`] || []);
  const lists = s.meta?.shortlists || [];
  const toggle = async (l) => {
    const on = inLists.has(l);
    try {
      await api(`/api/shortlist/${on ? 'remove' : 'add'}`, { name: l, kind: m.kind, ref: m.ref });
      toast(on ? `Removed from ${l}` : `Added to ${l}`); loadMarks(); loadMeta();
    } catch (e) { fail(e); }
  };
  const create = async (e) => {
    e.preventDefault();
    const n = name.trim().toLowerCase().replace(/\s+/g, '-');
    if (!n) return;
    try { await api('/api/shortlist', { name: n }); setName(''); await loadMeta(); await toggle(n); } catch (err) { fail(err); }
  };
  const left = Math.max(8, Math.min(m.x - 200, innerWidth - 268));
  const top = Math.min(m.y + 6, innerHeight - 80 - 32 * (lists.length + 1));
  return html`<div class="menu-backdrop" onClick=${() => setState({ starMenu: null })}>
    <div class="starmenu" style=${`left:${left}px;top:${Math.max(8, top)}px`} onClick=${(e) => e.stopPropagation()}>
      <div class="h">Shortlists</div>
      ${lists.map((l) => html`<label class="row"><input type="checkbox" checked=${inLists.has(l.name)} onChange=${() => toggle(l.name)} />
        <span class="grow">${l.name}</span><span class="faint">${l.count}</span></label>`)}
      ${!lists.length ? html`<div class="faint" style="font-size:12px">No shortlists yet.</div>` : null}
      <form class="row" onSubmit=${create}><input class="grow" placeholder="new-shortlist" value=${name} onInput=${(e) => setName(e.target.value)} />
        <button disabled=${!name.trim()}>Add</button></form>
    </div>
  </div>`;
}

// --- routing ----------------------------------------------------------------------------------

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = h.split('?');
  // the browser percent-encodes non-ASCII ids (ドット絵パック) in location.hash
  const dec = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
  const parts = path.split('/').map(dec);
  return { route: parts[0] || 'packs', rest: parts.slice(1).join('/'), params: new URLSearchParams(query) };
}
function useRoute() {
  const [r, setR] = useState(parseHash());
  useEffect(() => { const f = () => setR(parseHash()); addEventListener('hashchange', f); return () => removeEventListener('hashchange', f); }, []);
  return r;
}
function go(route, params = {}, replace = false) {
  const q = qs(params);
  const h = `#/${route}${q ? '?' + q : ''}`;
  if (replace) history.replaceState(null, '', h), dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = h;
}
const paramsObj = (p) => Object.fromEntries(p.entries());

// --- fonts ------------------------------------------------------------------------------------

const fontCache = new Map();
function loadFont(path) {
  if (!fontCache.has(path)) {
    const fam = 'f' + fontCache.size;
    fontCache.set(path, new FontFace(fam, `url("${libUrl(path)}")`).load().then((f) => { document.fonts.add(f); return fam; }).catch(() => null));
  }
  return fontCache.get(path);
}
function useFont(path, on = true) {
  const [fam, setFam] = useState(null);
  useEffect(() => { if (on && path) loadFont(path).then(setFam); }, [path, on]);
  return fam;
}

// --- audio player -----------------------------------------------------------------------------

const audio = new Audio();
audio.preload = 'auto';
// player = { item, get(i) -> Promise<item>, i, total, paused, time, dur, auto }
async function playAt(get, i, total) {
  const item = await get(i);
  if (!item) return;
  if (state.player?.item?.id === item.id) {
    if (audio.paused) audio.play(); else audio.pause();
    return;
  }
  audio.src = libUrl(item.path);
  audio.play().catch(() => {});
  setState({ player: { ...(state.player || { auto: prefs.autoNext ?? true }), item, get, i, total, paused: false } });
}
async function step(d) {
  const p = state.player;
  if (!p) return;
  for (let j = p.i + d, n = 0; j >= 0 && j < p.total && n < 200; j += d, n++) {
    const it = await p.get(j);
    if (it && it.media === 'audio' && it.available) return playAt(p.get, j, p.total);
  }
}
audio.addEventListener('play', () => state.player && setState({ player: { ...state.player, paused: false } }));
audio.addEventListener('pause', () => state.player && setState({ player: { ...state.player, paused: true } }));
audio.addEventListener('ended', () => { if (state.player?.auto) step(1); });
audio.addEventListener('error', () => state.player && toast(`Can't play ${state.player.item.name} in this browser`, true));

function Player() {
  const s = useStore();
  const [, tick] = useState(0);
  useEffect(() => {
    const f = () => tick((x) => x + 1);
    audio.addEventListener('timeupdate', f);
    return () => audio.removeEventListener('timeupdate', f);
  }, []);
  useEffect(() => { document.body.classList.toggle('has-player', !!s.player); }, [!!s.player]);
  if (!s.player) return null;
  const p = s.player;
  return html`<div class="player">
    <button onClick=${() => step(-1)} title="Previous">⏮</button>
    <button class="primary" onClick=${() => (audio.paused ? audio.play() : audio.pause())}>${p.paused ? '▶' : '❚❚'}</button>
    <button onClick=${() => step(1)} title="Next">⏭</button>
    <div class="t"><div>${p.item.name}</div><div class="dim" style="font-size:12px">${p.item.pack_name || ''}</div></div>
    <span class="mono dim">${secs(audio.currentTime)}</span>
    <input type="range" min="0" max=${audio.duration || 0} step="0.01" value=${audio.currentTime}
      onInput=${(e) => { audio.currentTime = +e.target.value; }} />
    <span class="mono dim">${secs(audio.duration || p.item.duration)}</span>
    <label class="row dim" style="font-size:12px"><input type="checkbox" checked=${p.auto}
      onChange=${(e) => { savePref('autoNext', e.target.checked); setState({ player: { ...p, auto: e.target.checked } }); }} /> auto-next</label>
    <button class="ghost" onClick=${() => { audio.pause(); setState({ player: null }); }} title="Close">✕</button>
  </div>`;
}

// --- shared pieces ----------------------------------------------------------------------------

function aiBadge(ai) {
  if (ai === 'no') return html`<span class="badge ok" title="Not AI-generated">human-made</span>`;
  if (ai === 'yes') return html`<span class="badge bad">AI-generated</span>`;
  return html`<span class="badge warn" title="AI status unknown">AI ?</span>`;
}
function licBadge(key) {
  const lic = state.meta?.licenses?.[key];
  const cls = key === 'unknown' ? 'bad' : '';
  return html`<span class="badge ${cls}" title=${lic?.summary || ''}>${key === 'unknown' ? 'license ?' : key}</span>`;
}

function PxImg({ src, w, h, boxW, boxH, onError, cls = '', palette }) {
  // Images smaller than the box are scaled up by whole numbers: nearest-neighbor for pixel art
  // (few colors), at most 2x and smooth for anti-aliased art.
  if (w && h && w <= boxW && h <= boxH) {
    const px = palette == null || palette <= 64;
    const k = Math.max(1, Math.min(px ? 99 : 2, Math.floor(Math.min(boxW / w, boxH / h))));
    return html`<img class="${px ? 'px' : ''} ${cls}" src=${src} width=${w * k} height=${h * k} loading="lazy" onError=${onError} />`;
  }
  return html`<img class=${cls} src=${src} loading="lazy" onError=${onError} />`;
}

function Cover({ id, kind, has }) {
  const [bad, setBad] = useState(false);
  const ref = useRef();
  if (!has || bad) return html`<div class="cover"><span class="glyph">${GLYPH[kind] || '·'}</span></div>`;
  return html`<div class="cover"><img ref=${ref} src=${coverUrl(id, 300)} loading="lazy" onError=${() => setBad(true)}
    onLoad=${(e) => { const i = e.target; if (i.naturalWidth * 1.5 < i.clientWidth) i.classList.add('px'); }} /></div>`;
}

function PackCard({ c }) {
  const n = c.children ? plural(c.children, 'pack') : plural(c.asset_count, 'asset');
  return html`<a class="card" href=${'#/pack/' + c.id}>
    <${Cover} id=${c.id} kind=${c.kind} has=${c.has_cover} />
    <div class="star"><${Star} kind="pack" refv=${c.id} /></div>
    <div class="body">
      <div class="name">${c.name}</div>
      <div class="sub">${c.group}</div>
      <div class="sub">${n} · ${bytes(c.total_size)}${c.pending ? ` · ${c.pending} not synced` : ''}</div>
      <div class="badges">
        <span class="badge kind">${GLYPH[c.kind] || ''} ${c.kind}</span>
        ${c.role === 'collection' ? html`<span class="badge">collection</span>` : null}
        ${styleTags(c.tags).length ? styleTags(c.tags).map((t) => html`<span class="badge">${t}</span>`)
          : c.pixel ? html`<span class="badge">pixel art</span>` : null}
        ${licBadge(c.license)} ${aiBadge(c.ai_generated)}
        ${c.engines ? html`<span class="badge">${c.engines}</span>` : null}
        ${c.also_in.length ? html`<span class="badge" title=${'Same files as: ' + c.also_in.join(', ')}>also elsewhere</span>` : null}
      </div>
    </div>
  </a>`;
}

const tagParts = (t) => { const i = t.indexOf(':'); return i < 0 ? [null, t] : [t.slice(0, i), t.slice(i + 1)]; };
const styleTags = (tags) => (tags || []).filter((t) => t.startsWith('style:')).map((t) => t.slice(6));

// Tags on the pack page: AI tags and yours look different; × removes, + adds, a removed AI tag
// stays visible (struck out) so one click brings it back. Every click is one curation edit.
function PackTags({ pk, onChange }) {
  const [adding, setAdding] = useState(false);
  const [v, setV] = useState('');
  const ai = new Set(pk.ai_tags || []);
  const tags = pk.tags || [];
  const removed = (pk.ai_tags || []).filter((t) => !tags.includes(t));
  const edit = async (add, remove) => {
    try { onChange(await api('/api/edit/tags', { id: pk.id, add, remove })); loadMeta(); } catch (e) { fail(e); }
  };
  const label = (t) => { const [ns, val] = tagParts(t); return html`${ns ? html`<span class="ns">${(NS_LABEL[ns] || ns).toLowerCase()}</span>` : null}${val}`; };
  const submit = (e) => { e.preventDefault(); if (v.trim()) edit([v.trim()], []); setV(''); setAdding(false); };
  return html`<div class="tags">
    ${tags.map((t) => html`<span class="tag ${ai.has(t) ? 'ai' : 'own'}" title=${ai.has(t) ? `AI tag (${pk.ai_model})` : 'Your tag'}>
      <a href=${'#/packs?tag=' + enc(t)}>${label(t)}</a><button class="x" title="Remove" onClick=${() => edit([], [t])}>×</button></span>`)}
    ${removed.map((t) => html`<span class="tag removed" title="AI tag you removed. Click to restore it."><a onClick=${() => edit([t], [])}>${label(t)}</a></span>`)}
    ${adding
      ? html`<form onSubmit=${submit}><input class="tagin" list="tagvalues" autofocus value=${v} placeholder="style:pixel-art, or any word"
          onInput=${(e) => setV(e.target.value)} onKeyDown=${(e) => e.key === 'Escape' && setAdding(false)} onBlur=${() => !v && setAdding(false)} />
          <datalist id="tagvalues">${(state.meta?.tag_values || []).map((x) => html`<option value=${x} />`)}</datalist></form>`
      : html`<button class="tag add" onClick=${() => setAdding(true)}>+ tag</button>`}
    ${pk.ai_model ? html`<span class="faint" style="font-size:11px" title="Dashed tags come from the AI. Yours are solid.">AI: ${pk.ai_model}, ${(pk.ai_at || '').slice(0, 10)}${pk.ai_stale ? ' · outdated' : ''}</span>` : null}
  </div>`;
}

function Chips({ options, value, onChange, counts, labels }) {
  return html`<div class="chips">${options.map((o) => html`<span class="chip ${value === o ? 'on' : ''}"
    onClick=${() => onChange(value === o ? '' : o)}>${labels?.[o] || o}${counts?.[o] != null ? html`<small>${counts[o]}</small>` : null}</span>`)}</div>`;
}

// Filter headings say what they mean and where the values come from (hover the ⓘ).
const NS_LABEL = { kind: 'Content', style: 'Style', perspective: 'Perspective', genre: 'Genre', mood: 'Mood' };
const NS_HELP = {
  kind: 'What the pack is for: tilesets, characters, UI, icons… Tags from the AI pack classification, plus the ones you added.',
  style: 'How the art looks: pixel art, vector, hand-drawn… Tags from the AI pack classification, plus yours.',
  perspective: 'The view the art is drawn for: top-down, side, isometric. Tags from the AI pack classification, plus yours.',
  genre: 'Setting or theme: fantasy, sci-fi, modern… Tags from the AI pack classification, plus yours.',
};
const AI_LABEL = { no: 'human-made', unknown: 'unknown', yes: 'AI-made' };
function FacetLabel({ label, help }) {
  return html`<label class="h" title=${help}>${label} <span class="help">ⓘ</span></label>`;
}

// --- packs wall -------------------------------------------------------------------------------

function PacksPage({ params }) {
  const s = useStore();
  const p = paramsObj(params);
  const [data, setData] = useState(null);
  const [shown, setShown] = useState(120);
  const sentinel = useRef();
  const key = params.toString();
  useEffect(() => {
    setData(null); setShown(120);
    api(`/api/packs?${qs({ ...p, copies: p.copies === '1', sort: p.sort || (p.q ? 'relevance' : 'name') })}`).then(setData, fail);
  }, [key]);
  useEffect(() => {
    if (!sentinel.current) return;
    const io = new IntersectionObserver((es) => { if (es[0].isIntersecting) setShown((n) => n + 120); }, { rootMargin: '600px' });
    io.observe(sentinel.current);
    return () => io.disconnect();
  }, [data, shown]);
  const set = (k, v) => go('packs', { ...p, [k]: v, ...(k === 'sort' && v === 'random' ? { seed: Math.floor(Math.random() * 1e6) } : {}) }, true);
  const m = s.meta;
  if (!m) return null;
  const groups = m.groups.filter((g) => !p.section || g.section === p.section);
  const tagsOn = (p.tag || '').split(',').filter(Boolean);
  const setTags = (ts) => set('tag', ts.join(','));
  const facetNs = new Set((m.tag_facets || []).map((f) => f.ns));
  const otherTags = tagsOn.filter((t) => !facetNs.has(tagParts(t)[0]));
  return html`<div class="packs">
    <aside class="facets">
      <div><label class="h">Search packs</label>
        <input type="search" placeholder="name, contents, readme…" value=${p.q || ''}
          onChange=${(e) => set('q', e.target.value)} /></div>
      <div><label class="h">Sort</label>
        <div class="row">
          <select class="grow" value=${p.sort || (p.q ? 'relevance' : 'name')} onChange=${(e) => set('sort', e.target.value)}>
            ${p.q ? html`<option value="relevance">Relevance</option>` : null}
            <option value="name">Name</option><option value="group">Bundle</option><option value="recent">Newest files</option>
            <option value="size">Size</option><option value="assets">Asset count</option><option value="random">Shuffle</option>
          </select>
          <button title="New shuffle" onClick=${() => set('sort', 'random')}>Shuffle</button>
        </div></div>
      ${otherTags.length ? html`<div><${FacetLabel} label="Tagged" help="Packs with this tag (yours or the AI's). Click to remove the filter." />
        <div class="chips">${otherTags.map((t) => html`<span class="chip on" onClick=${() => setTags(tagsOn.filter((x) => x !== t))}>${t} ✕</span>`)}</div></div>` : null}
      <div class="fgroup"><div class="gh">Library</div>
        <div><${FacetLabel} label="Section" help="The top-level folders of your library, as set in the profile's catalog.toml." />
          <${Chips} options=${m.sections || []} value=${p.section || ''} onChange=${(v) => go('packs', { ...p, section: v, group: '' }, true)} /></div>
        <div><${FacetLabel} label="Bundle / store" help="Where packs came from: a bundle or a store's folder. Each has its own curation file with default license and AI status." />
          <select value=${p.group || ''} onChange=${(e) => set('group', e.target.value)}>
            <option value="">All</option>
            ${groups.map((g) => html`<option value=${g.id}>${g.name} (${g.packs})</option>`)}
          </select></div>
      </div>
      <div class="fgroup"><div class="gh">What's in it</div>
        <div><${FacetLabel} label="Media" help="What the files are, worked out from their formats when indexing: graphics, sound effects, music, fonts, 3D models. Every pack has one." />
          <${Chips} options=${Object.keys(m.kinds)} counts=${m.kinds} value=${p.kind || ''} onChange=${(v) => set('kind', v)} /></div>
        ${(m.tag_facets || []).map((f) => html`<div><${FacetLabel} label=${NS_LABEL[f.ns] || f.ns} help=${NS_HELP[f.ns] || 'Tags from the AI pack classification, plus the ones you added.'} />
          <${Chips} options=${Object.keys(f.counts)} counts=${f.counts} value=${tagsOn.find((t) => t.startsWith(f.ns + ':'))?.slice(f.ns.length + 1) || ''}
            onChange=${(v) => setTags([...tagsOn.filter((t) => !t.startsWith(f.ns + ':')), ...(v ? [f.ns + ':' + v] : [])])} /></div>`)}
        ${(m.tag_facets || []).some((f) => f.ns === 'style') ? null : html`<div><${FacetLabel} label="Pixel art (guess)"
            help="Guessed from image sizes and colors. Once packs have AI tags (assetcat classify), a Style filter replaces this." />
          <${Chips} options=${['pixel', 'not-pixel']} value=${p.style || ''} onChange=${(v) => set('style', v)} /></div>`}
      </div>
      <div class="fgroup"><div class="gh">Rights</div>
        <div><${FacetLabel} label="License" help="From your curation files: the pack's own license, or its bundle's default." />
          <select value=${p.license || ''} onChange=${(e) => set('license', e.target.value)}>
            <option value="">All</option>
            ${Object.entries(m.license_counts).map(([k, n]) => html`<option value=${k}>${m.licenses[k]?.name || k} (${n})</option>`)}
          </select></div>
        <div><${FacetLabel} label="Made by" help="Whether the art is AI-generated, as you recorded it in curation (with a reason). Human-made means ai_generated = no." />
          <${Chips} options=${['no', 'unknown', 'yes']} labels=${AI_LABEL} counts=${m.ai} value=${p.ai || ''} onChange=${(v) => set('ai', v)} /></div>
      </div>
      <div class="fgroup">
        <label class="row dim" title="Also list the packs inside collections, not just the collections"><input type="checkbox" checked=${!!p.sub} onChange=${(e) => set('sub', e.target.checked)} /> include sub-packs</label>
        <label class="row dim" title="Hide packs whose files are (almost all) in another pack shown here, e.g. the same pack in two bundles">
          <input type="checkbox" checked=${p.copies !== '1'} onChange=${(e) => set('copies', e.target.checked ? '' : '1')} /> hide copies</label>
      </div>
      <div class="faint" style="font-size:12px">${m.stats.packs.toLocaleString()} packs · ${m.stats.assets.toLocaleString()} assets · ${bytes(m.stats.bytes)}<br/>indexed ${m.indexed_at}</div>
    </aside>
    <section>
      <div class="row dim" style="margin-bottom:10px">${data ? plural(data.total, 'pack') : 'Loading…'}${data?.hidden_copies ? html` <span class="faint">(${data.hidden_copies} copies hidden)</span>` : null}</div>
      ${data && !data.total ? html`<div class="empty">No packs match.</div>` : null}
      <div class="cards">${(data?.items || []).slice(0, shown).map((c) => html`<${PackCard} key=${c.id} c=${c} />`)}</div>
      ${data && shown < data.total ? html`<div class="more" ref=${sentinel}><button onClick=${() => setShown(shown + 240)}>More</button></div>` : null}
    </section>
  </div>`;
}

// --- virtualized asset grid -------------------------------------------------------------------

const PAGE = 300;
const GAP = 8;
const CAP = 36;

function AssetGrid({ params, size = 128, bg = 'checker', showPack = false, onTotal, list = false }) {
  const s = useStore();
  const box = useRef();
  const key = JSON.stringify(params);
  const keyRef = useRef(key);
  const pages = useRef(new Map());
  const [total, setTotal] = useState(null);
  const [, setTick] = useState(0);
  const [view, setView] = useState({ top: 0, bottom: 1200, width: 800 });

  const load = useCallback((page) => {
    const k = keyRef.current;
    if (pages.current.has(page)) return pages.current.get(page);
    const pr = api(`/api/assets?${qs({ ...params, offset: page * PAGE, limit: PAGE })}`).then((d) => {
      if (keyRef.current !== k) return [];
      pages.current.set(page, d.items);
      setTotal(d.total); onTotal?.(d.total);
      setTick((t) => t + 1);
      return d.items;
    }, (e) => { fail(e); pages.current.delete(page); return []; });
    pages.current.set(page, pr);
    return pr;
  }, [key]);

  const get = useCallback(async (i) => {
    let pg = pages.current.get(Math.floor(i / PAGE));
    if (!pg || pg.then) pg = await load(Math.floor(i / PAGE));
    return pg[i % PAGE];
  }, [load]);

  useEffect(() => {
    keyRef.current = key; pages.current = new Map(); setTotal(null); load(0);
  }, [key]);

  useEffect(() => {
    const measure = () => {
      const el = box.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setView({ top: Math.max(0, -r.top), bottom: Math.max(0, -r.top) + innerHeight, width: el.clientWidth });
    };
    measure();
    addEventListener('scroll', measure, { passive: true });
    addEventListener('resize', measure);
    const ro = new ResizeObserver(measure);
    ro.observe(box.current);
    return () => { removeEventListener('scroll', measure); removeEventListener('resize', measure); ro.disconnect(); };
  }, []);

  const cols = list ? 1 : Math.max(1, Math.floor((view.width + GAP) / (size + GAP)));
  const tw = (view.width - GAP * (cols - 1)) / cols;
  const th = list ? 40 : size + CAP;
  const rowH = th + (list ? 4 : GAP);
  const n = total || 0;
  const rows = Math.ceil(n / cols);
  const first = Math.max(0, Math.floor(view.top / rowH) - 3);
  const last = Math.min(rows - 1, Math.ceil(view.bottom / rowH) + 3);
  const tiles = [];
  for (let i = first * cols; i < Math.min(n, (last + 1) * cols); i++) {
    const pg = pages.current.get(Math.floor(i / PAGE));
    if (!pg) load(Math.floor(i / PAGE));
    const item = Array.isArray(pg) ? pg[i % PAGE] : null;
    const style = `left:${(i % cols) * (tw + GAP)}px;top:${Math.floor(i / cols) * rowH}px;width:${tw}px;height:${th}px`;
    tiles.push(item
      ? html`<${list ? Row : Tile} key=${item.id} item=${item} style=${style} w=${tw} h=${size} bg=${bg} showPack=${showPack}
          playing=${s.player?.item?.id === item.id && !s.player.paused}
          onOpen=${() => setState({ viewer: { get, i, total: n } })}
          onPlay=${() => playAt(get, i, n)} />`
      : html`<div key=${'p' + i} class="tile" style=${style}></div>`);
  }
  return html`<div>
    ${total === 0 ? html`<div class="empty">Nothing here.</div>` : null}
    <div class="vgrid" ref=${box} style=${`height:${rows * rowH}px`}>${tiles}</div>
  </div>`;
}

function Row({ item, style, bg, showPack, playing, onOpen, onPlay }) {
  const [bad, setBad] = useState(false);
  const fam = useFont(item.path, item.media === 'font');
  let lead;
  if (item.media === 'audio') lead = html`<button class="ghost rowplay" onClick=${(e) => { e.stopPropagation(); onPlay(); }}>${playing ? '❚❚' : '▶'}</button>`;
  else if (item.media === 'model') lead = html`<div class="rowthumb bg-${bg}"><${ModelThumb} item=${item} px=${32} /></div>`;
  else if (item.media === 'visual' && !bad) lead = html`<div class="rowthumb bg-${bg}"><${PxImg} src=${thumbUrl(item.path, 32)} w=${item.width} h=${item.height} palette=${item.palette} boxW=${32} boxH=${32} onError=${() => setBad(true)} /></div>`;
  else lead = html`<div class="rowthumb"><span class="faint mono" style="font-size:10px">${item.ext}</span></div>`;
  const meta = [item.type, item.duration ? secs(item.duration) : '', item.width ? `${item.width}×${item.height}` : '',
    ...(item.media === 'model' ? modelBits(item) : []), item.variants > 1 ? item.formats : ''].filter(Boolean);
  return html`<div class="row-item tile ${playing ? 'playing' : ''} ${item.available ? '' : 'pending'}" style=${style}
      onClick=${() => (item.media === 'audio' ? onPlay() : onOpen())}>
    ${lead}
    <div class="grow" style="min-width:0">
      <div class="n" style=${fam ? `font-family:${fam};font-size:16px` : ''}>${item.name}</div>
      <div class="m faint">${showPack ? item.pack_name + ' / ' : ''}${item.folder}</div>
    </div>
    <span class="faint" style="font-size:12px;white-space:nowrap">${meta.join(' · ')}</span>
    <button class="ghost" title="Details" onClick=${(e) => { e.stopPropagation(); onOpen(); }}>ⓘ</button>
  </div>`;
}

// 3D: three.js (vendor/three) loads on the first model shown. A model's tile shows its bundled
// render or a render cached by the server; failing both, one is rendered here (one at a time, a
// single shared WebGL context) and posted back to the server's cache.
let three = null;
const load3d = () => (three ||= import('./model3d.js'));
const MODEL_EXT = new Set(['glb', 'gltf', 'obj', 'fbx']);
const renderQueue = [];
let rendering = false;
async function pumpRenders() {
  if (rendering) return;
  rendering = true;
  while (renderQueue.length) {
    const job = renderQueue.pop(); // newest first: the tiles just scrolled to
    if (!job.live()) continue;
    try {
      const m = await load3d();
      const blob = await m.renderThumb(libUrl(job.path), job.ext, 384);
      fetch(`/api/render?path=${enc(job.path)}`, { method: 'POST', body: blob }).catch(() => {});
      job.done(URL.createObjectURL(blob));
    } catch (e) {
      console.warn(`3D render failed: ${job.path}: ${e.message || e}`);
      job.done(null);
    }
  }
  rendering = false;
}

function ModelThumb({ item, px }) {
  const [src, setSrc] = useState(() => thumbUrl(item.path, px));
  const [failed, setFailed] = useState(false);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => () => { if (src?.startsWith('blob:')) URL.revokeObjectURL(src); }, [src]);
  if (failed) return html`<span class="fmt">◆ ${item.ext}</span>`;
  if (!src) return html`<span class="fmt rendering" title="Rendering a preview…">◆ ${item.ext}</span>`;
  const onError = () => {
    if (src.startsWith('blob:') || !MODEL_EXT.has(item.ext)) return setFailed(true);
    setSrc(null);
    renderQueue.push({ path: item.path, ext: item.ext, live: () => live.current,
      done: (u) => { if (live.current) (u ? setSrc(u) : setFailed(true)); else if (u) URL.revokeObjectURL(u); } });
    pumpRenders();
  };
  return html`<img src=${src} loading="lazy" onError=${onError} />`;
}

const modelBits = (item) => [item.tris ? `${item.tris.toLocaleString()} tris` : '', item.clips ? plural(item.clips, 'clip') : '',
  item.rigged ? 'rigged' : ''].filter(Boolean);

// Waveforms: peaks from the server (/wave, cached there), fetched a few at a time.
const waves = new Map();
const waveQueue = [];
let waveBusy = 0;
function fetchWave(path) {
  if (waves.has(path)) return waves.get(path);
  const pr = new Promise((resolve) => waveQueue.push({ path, resolve }));
  waves.set(path, pr);
  pumpWaves();
  return pr;
}
function pumpWaves() {
  while (waveBusy < 4 && waveQueue.length) {
    const { path, resolve } = waveQueue.shift();
    waveBusy++;
    fetch(`/wave?path=${enc(path)}`).then((r) => (r.ok ? r.json() : [])).catch(() => []).then((d) => {
      waves.set(path, d); resolve(d); waveBusy--; pumpWaves();
    });
  }
}
function Wave({ path, w, h, playing }) {
  const [peaks, setPeaks] = useState(Array.isArray(waves.get(path)) ? waves.get(path) : null);
  useEffect(() => {
    let live = true;
    if (!peaks) Promise.resolve(fetchWave(path)).then((d) => live && setPeaks(d));
    return () => { live = false; };
  }, [path]);
  if (!peaks || !peaks.length) return html`<span class="audio">♪</span>`;
  const n = peaks.length, bw = w / n, mid = h / 2;
  let d = '';
  peaks.forEach((v, i) => { const y = Math.max(0.5, v * mid); d += `M${(i * bw + bw / 2).toFixed(1)} ${(mid - y).toFixed(1)}V${(mid + y).toFixed(1)}`; });
  return html`<svg class="wave ${playing ? 'playing' : ''}" width=${w} height=${h} viewBox=${`0 0 ${w} ${h}`}>
    <path d=${d} stroke-width=${Math.max(1, bw * 0.7).toFixed(1)} /></svg>`;
}

function Tile({ item, style, w, h, bg, showPack, playing, onOpen, onPlay }) {
  const [bad, setBad] = useState(false);
  const fam = useFont(item.path, item.media === 'font');
  let body;
  const meta = [];
  if (item.media === 'visual') {
    body = bad || NO_THUMB.has(item.ext)
      ? html`<span class="fmt">${item.ext}</span>`
      : html`<${PxImg} src=${thumbUrl(item.path, Math.max(w, h))} w=${item.width} h=${item.height} palette=${item.palette} boxW=${w - 8} boxH=${h - 8} onError=${() => setBad(true)} />`;
    if (item.width) meta.push(`${item.width}×${item.height}`);
    if (item.grid_w) meta.push(`grid ${item.grid_w}×${item.grid_h}`);
    else if (item.frames) meta.push(`${item.frames} fr`);
  } else if (item.media === 'audio') {
    body = html`<${Wave} path=${item.path} w=${w - 16} h=${Math.round((h - 8) * 0.6)} playing=${playing} />
      <span class="audio-play">${playing ? '❚❚' : '▶'}</span>`;
    meta.push(secs(item.duration), item.type);
  } else if (item.media === 'font') {
    body = html`<span class="font" style=${fam ? `font-family:${fam}` : ''}>Aa Bb 12</span>`;
    meta.push(item.ext);
  } else if (item.media === 'model') {
    body = html`<${ModelThumb} item=${item} px=${Math.max(w, h)} />`;
    meta.push(...modelBits(item));
  } else {
    body = html`<span class="fmt">${item.ext}</span>`;
  }
  if (item.variants > 1) meta.push(item.formats.includes(',') ? item.formats : `${item.variants} variants`);
  if (showPack) meta.unshift(item.pack_name);
  const cls = `tile ${playing ? 'playing' : ''} ${item.available ? '' : 'pending'}`;
  const click = () => (item.media === 'audio' ? onPlay() : onOpen());
  return html`<div class=${cls} style=${style} onClick=${click} title=${item.inner || item.path}>
    <div class="img bg-${item.media === 'visual' || item.media === 'model' ? bg : 'none'}">${body}</div>
    ${item.media === 'audio' ? html`<button class="info ghost" onClick=${(e) => { e.stopPropagation(); onOpen(); }}>ⓘ</button>` : null}
    <div class="cap"><div class="n">${item.name}</div><div class="m">${meta.filter(Boolean).join(' · ') || ' '}</div></div>
  </div>`;
}

// --- viewer -----------------------------------------------------------------------------------

function fitGrid(W, H, g) {
  const { w, h, spacing: s = 0, margin: m = 0 } = g;
  if (!(W && H && w > 0 && h > 0)) return null;
  const iw = W - 2 * m + s, ih = H - 2 * m + s;
  if (iw <= 0 || ih <= 0 || iw % (w + s) || ih % (h + s)) return null;
  return { cols: iw / (w + s), rows: ih / (h + s) };
}

function GridOverlay({ W, H, g, k }) {
  const f = fitGrid(W, H, g);
  if (!f) return null;
  const { w, h, spacing: s = 0, margin: m = 0 } = g;
  const lines = [];
  if (f.cols * f.rows <= 4000) {
    for (let r = 0; r < f.rows; r++)
      for (let c = 0; c < f.cols; c++)
        lines.push(html`<rect x=${m + c * (w + s)} y=${m + r * (h + s)} width=${w} height=${h} />`);
  }
  return html`<svg width=${W * k} height=${H * k} viewBox=${`0 0 ${W} ${H}`}>
    <g fill="none" stroke="rgba(232,163,61,.85)" stroke-width="1" vector-effect="non-scaling-stroke" style="vector-effect:non-scaling-stroke">${lines}</g>
  </svg>`;
}

function Animator({ src, W, H, g }) {
  const f = fitGrid(W, H, g);
  const cv = useRef();
  const img = useRef();
  const [start, setStart] = useState(0);
  const [count, setCount] = useState(f ? f.cols : 1);
  const [fps, setFps] = useState(8);
  const [playing, setPlaying] = useState(true);
  useEffect(() => { if (f) { setStart(0); setCount(f.cols); } }, [g.w, g.h, g.spacing, g.margin]);
  useEffect(() => {
    if (!f) return;
    const im = new Image();
    im.src = src;
    img.current = im;
    let frame = 0, t;
    const k = Math.max(1, Math.floor(160 / Math.max(g.w, g.h)));
    const draw = () => {
      const c = cv.current;
      if (!c || !im.complete) return;
      const total = f.cols * f.rows;
      const idx = (start + (frame % Math.max(1, count))) % total;
      const cx = idx % f.cols, cy = Math.floor(idx / f.cols);
      const sx = (g.margin || 0) + cx * (g.w + (g.spacing || 0)), sy = (g.margin || 0) + cy * (g.h + (g.spacing || 0));
      c.width = g.w * k; c.height = g.h * k;
      const ctx = c.getContext('2d');
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.drawImage(im, sx, sy, g.w, g.h, 0, 0, g.w * k, g.h * k);
    };
    im.onload = draw;
    draw();
    if (playing) t = setInterval(() => { frame++; draw(); }, 1000 / fps);
    return () => clearInterval(t);
  }, [src, g.w, g.h, g.spacing, g.margin, start, count, fps, playing]);
  if (!f) return html`<div class="faint">Set a grid that fits the image to animate it.</div>`;
  const row = Math.floor(start / f.cols);
  return html`<div class="anim">
    <canvas ref=${cv} class="bg-checker"></canvas>
    <div style="display:flex;flex-direction:column;gap:6px;font-size:12px">
      <label class="row">row <select value=${start % f.cols === 0 && count === f.cols ? row : ''} onChange=${(e) => { setStart(+e.target.value * f.cols); setCount(f.cols); }}>
        <option value="">custom</option>
        ${Array.from({ length: f.rows }, (_, r) => html`<option value=${r}>${r + 1}</option>`)}</select></label>
      <label class="row">first cell <input type="number" min="0" max=${f.cols * f.rows - 1} value=${start} onInput=${(e) => setStart(+e.target.value || 0)} /></label>
      <label class="row">frames <input type="number" min="1" max=${f.cols * f.rows} value=${count} onInput=${(e) => setCount(Math.max(1, +e.target.value || 1))} /></label>
      <label class="row">${fps} fps <input type="range" min="1" max="30" value=${fps} onInput=${(e) => setFps(+e.target.value)} /></label>
      <button onClick=${() => setPlaying(!playing)}>${playing ? 'Pause' : 'Play'}</button>
    </div>
  </div>`;
}

const fmtNum = (x) => (x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toPrecision(2));

function ModelStage({ a, bg, setBg }) {
  const el = useRef();
  const stage = useRef(null);
  const [ready, setReady] = useState(false);
  const files = a.files.filter((f) => MODEL_EXT.has(f.ext));
  const [file, setFile] = useState(null);
  const [info, setInfo] = useState(null);
  const [err, setErr] = useState(null);
  const [clip, setClip] = useState('');
  const [paused, setPaused] = useState(false);
  const [wire, setWire] = useState(false);
  const [grid, setGrid] = useState(prefs.modelGrid ?? true);
  const [tex, setTex] = useState('');

  useEffect(() => {
    let live = true;
    load3d().then((m) => { if (live) { stage.current = new m.Stage(el.current); setReady(true); } }, (e) => setErr(String(e)));
    return () => { live = false; stage.current?.dispose(); stage.current = null; };
  }, []);
  useEffect(() => { setFile(files.some((f) => f.path === a.path) ? a.path : files[0]?.path); setTex(''); }, [a.id]);
  useEffect(() => {
    if (!ready || !file) return;
    let live = true;
    setInfo(null); setErr(null); setClip(''); setPaused(false); setTex('');
    const ext = file.split('.').pop().toLowerCase();
    load3d().then((m) => m.loadModel(libUrl(file), ext).then((model) => {
      if (!live || !stage.current) return m.dispose(model.root);
      const st = stage.current.show(model);
      stage.current.setGrid(grid);
      const names = model.clips.map((c) => c.name);
      setInfo({ ...st, clips: names });
      const first = names.find((n) => /idle/i.test(n)) || names.find((n) => !/^(static|t-?pose)$/i.test(n)) || names[0] || '';
      setClip(first);
      stage.current.play(first);
    })).catch((e) => live && setErr(e.message || String(e)));
    return () => { live = false; };
  }, [ready, file]);

  const ext = file ? file.split('.').pop().toLowerCase() : a.ext;
  const missing = a.model?.[file]?.missing || [];
  const inner = (p) => p.slice(a.pack_path.length + 1);
  return html`<div class="m3d bg-${bg}" ref=${el}>
    <div class="tools">
      <select value=${bg} onChange=${(e) => { setBg(e.target.value); savePref('viewerBg', e.target.value); }}>
        <option value="checker">Checker</option><option value="dark">Dark</option><option value="light">Light</option><option value="magenta">Magenta</option>
      </select>
      ${files.length > 1 ? html`<select value=${file} title="Which file to show" onChange=${(e) => setFile(e.target.value)}>
        ${files.map((f) => html`<option value=${f.path}>${f.ext.toUpperCase()}</option>`)}</select>` : null}
      <button onClick=${() => stage.current?.reset()} title="Back to the starting view">Reset view</button>
      <button onClick=${() => { const v = !grid; setGrid(v); savePref('modelGrid', v); stage.current?.setGrid(v); }}>${grid ? 'Hide floor' : 'Show floor'}</button>
      <button onClick=${() => { setWire(!wire); stage.current?.wire(!wire); }}>${wire ? 'Solid' : 'Wireframe'}</button>
      ${info?.clips.length ? html`<select value=${clip} title="Animation clip" onChange=${(e) => { setClip(e.target.value); stage.current?.play(e.target.value); }}>
        <option value="">No animation</option>${info.clips.map((c) => html`<option value=${c}>${c}</option>`)}</select>
        <button onClick=${() => { setPaused(!paused); stage.current?.setPaused(!paused); }}>${paused ? 'Play' : 'Pause'}</button>` : null}
      ${ext === 'fbx' && a.textures?.length && info?.meshes ? html`<select value=${tex} title="FBX files often point at textures on the artist's machine: try one from the pack"
        onChange=${(e) => { setTex(e.target.value); stage.current?.retexture(e.target.value ? libUrl(e.target.value) : null).catch(fail); }}>
        <option value="">Texture: as in the file</option>${a.textures.map((t) => html`<option value=${t}>${inner(t)}</option>`)}</select>` : null}
    </div>
    ${err ? html`<div class="m3d-msg err">Can't show this model: ${err}</div>` : !info ? html`<div class="m3d-msg">Loading…</div>` : null}
    ${info ? html`<div class="m3d-info">${[`${info.tris.toLocaleString()} triangles`, plural(info.meshes, 'mesh').replace('meshs', 'meshes'),
      plural(info.materials, 'material'), plural(info.textures, 'texture'), `${info.size.map(fmtNum).join(' × ')} units`].join(' · ')}
      ${missing.length ? html`<div class="err">Missing: ${missing.map(inner).join(', ')}</div>` : null}
      ${!info.meshes && info.bones ? html`<div>No meshes: this file holds a skeleton and its animation, shown as bones.</div>` : null}
      ${ext === 'fbx' ? html`<div class="faint">FBX preview is best effort: textures may not load.</div>` : null}</div>` : null}
  </div>`;
}

function Viewer() {
  const s = useStore();
  const v = s.viewer;
  const [i, setI] = useState(v.i);
  const [a, setA] = useState(null);
  const [zoom, setZoom] = useState('fit');
  const [bg, setBg] = useState(prefs.viewerBg || 'checker');
  const [showGrid, setShowGrid] = useState(true);
  const [g, setG] = useState(null);
  const [stage, setStage] = useState({ w: 800, h: 600 });
  const stageRef = useRef();
  const fam = useFont(a?.path, a?.media === 'font');
  const [sample, setSample] = useState('The quick brown fox jumps over the lazy dog 0123456789');

  useEffect(() => { setI(v.i); }, [v]);
  useEffect(() => {
    let live = true;
    (async () => {
      const it = await v.get(i);
      if (!it || !live) return;
      const d = await api(`/api/asset?id=${it.id}`).catch(fail);
      if (!live || !d) return;
      setA(d);
      setG(d.grid_w ? { w: d.grid_w, h: d.grid_h, spacing: d.grid_spacing || 0, margin: d.grid_margin || 0 } : null);
    })();
    return () => { live = false; };
  }, [i, v]);
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setStage({ w: el.clientWidth - 40, h: el.clientHeight - 40 }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const close = () => setState({ viewer: null });
  const nav = (d) => { const j = i + d; if (j >= 0 && j < v.total) setI(j); };
  useEffect(() => {
    const f = (e) => {
      if (e.target.matches('input, textarea, select')) return;
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowRight') nav(1);
      else if (e.key === 'ArrowLeft') nav(-1);
      else if (e.key === 'g') setShowGrid((x) => !x);
      else if (e.key === '0') setZoom('fit');
      else if (/^[1-8]$/.test(e.key)) setZoom(+e.key);
    };
    addEventListener('keydown', f);
    return () => removeEventListener('keydown', f);
  }, [i, v]);

  if (!a) return html`<div class="modal" onClick=${close}><div class="empty">Loading…</div></div>`;
  const W = a.width, H = a.height;
  const src = a.displayable ? libUrl(a.path) : thumbUrl(a.path, 512);
  let k = 1, smooth = false;
  if (W && H) {
    if (zoom === 'fit') {
      const f = Math.min(stage.w / W, stage.h / H);
      k = f >= 1 ? Math.floor(f) : f;
      smooth = f < 1;
    } else k = zoom;
  }
  const gEdit = g || { w: '', h: '', spacing: 0, margin: 0 };
  const fits = g && fitGrid(W, H, g);
  const setGv = (key, val) => setG({ ...gEdit, [key]: val === '' ? '' : +val, ...(key === 'w' && (!gEdit.h || gEdit.h === gEdit.w) ? { h: +val } : {}) });
  const saveGrid = async (spec) => {
    try {
      const d = await api('/api/edit/grid', { asset_id: a.id, grid: spec });
      setA(d); toast(spec ? 'Grid saved to curation' : 'Grid removed');
    } catch (e) { fail(e); }
  };
  const inner = a.inner;
  let stageBody;
  if (a.media === 'visual') {
    stageBody = html`<div class="canvas bg-${bg}">
      <img src=${src} class=${smooth ? 'smooth' : ''} width=${W ? W * k : undefined} height=${H ? H * k : undefined} />
      ${showGrid && g ? html`<${GridOverlay} W=${W} H=${H} g=${g} k=${k} />` : null}
    </div>`;
  } else if (a.media === 'audio') {
    stageBody = html`<audio controls autoplay src=${libUrl(a.path)} style="width:min(600px,90%)"></audio>`;
  } else if (a.media === 'font') {
    stageBody = html`<div style="width:100%;display:flex;flex-direction:column;gap:10px;align-items:center">
      <input style="width:min(700px,90%)" value=${sample} onInput=${(e) => setSample(e.target.value)} />
      ${[56, 32, 18].map((px) => html`<div class="fontsample" style=${`font-family:${fam || 'inherit'};font-size:${px}px`}>${sample}</div>`)}
    </div>`;
  } else if (a.media === 'model' && a.files.some((f) => MODEL_EXT.has(f.ext))) {
    stageBody = html`<${ModelStage} a=${a} bg=${bg} setBg=${setBg} />`;
  } else {
    stageBody = html`<div class="empty">No preview for .${a.ext} files${a.media === 'model' ? ' (3D previews: glTF/GLB, OBJ and FBX)' : ''}.</div>`;
  }
  return html`<div class="modal">
    <div class="viewer">
      <div class="stage ${a.media === 'model' ? 'stage3d' : ''}" ref=${stageRef} onClick=${(e) => { if (e.target === e.currentTarget) close(); }}>
        ${a.media === 'visual' ? html`<div class="tools">
          <select value=${zoom} onChange=${(e) => setZoom(e.target.value === 'fit' ? 'fit' : +e.target.value)}>
            <option value="fit">Fit</option>${[1, 2, 3, 4, 6, 8, 12, 16].map((z) => html`<option value=${z}>${z}×</option>`)}
          </select>
          <select value=${bg} onChange=${(e) => { setBg(e.target.value); savePref('viewerBg', e.target.value); }}>
            <option value="checker">Checker</option><option value="dark">Dark</option><option value="light">Light</option><option value="magenta">Magenta</option>
          </select>
          ${g ? html`<button onClick=${() => setShowGrid(!showGrid)}>${showGrid ? 'Hide grid' : 'Show grid'}</button>` : null}
        </div>` : null}
        ${stageBody}
        ${i > 0 ? html`<button class="navbtn" style="left:12px" onClick=${() => nav(-1)}>‹</button>` : null}
        ${i < v.total - 1 ? html`<button class="navbtn" style="right:352px" onClick=${() => nav(1)}>›</button>` : null}
      </div>
      <div class="side">
        <div class="row"><h3 class="grow">${a.name}</h3><${Star} kind="asset" refv=${a.path} /><button class="ghost" onClick=${close} title="Close (Esc)">✕</button></div>
        <div class="kv">
          <span>Pack</span><span><a href=${'#/pack/' + a.pack_id} onClick=${close}>${a.pack_name}</a></span>
          <span>Folder</span><span class="mono">${a.folder || '/'}</span>
          <span>Type</span><span>${a.type} (${a.media})</span>
          ${a.subjects?.length ? html`<span>Shows</span><span title="Scored by the image model">${a.subjects.map((x) => `${x.subject.replace(/-/g, ' ')} (${Math.round(x.score * 100)}%)`).join(', ')}</span>` : null}
          ${W ? html`<span>Size</span><span>${W} × ${H}</span>` : null}
          ${a.palette != null && a.media === 'visual' ? html`<span>Colors</span><span>${a.palette > 256 ? '256+' : a.palette}</span>` : null}
          ${a.frames ? html`<span>Frames</span><span>${a.frames}${a.atlas ? ' (atlas)' : ''}</span>` : null}
          ${a.duration ? html`<span>Length</span><span>${secs(a.duration)}</span>` : null}
          ${a.files[0]?.channels ? html`<span>Audio</span><span>${a.files[0].channels === 1 ? 'mono' : 'stereo'}, ${a.files[0].sample_rate} Hz</span>` : null}
          ${a.font_family ? html`<span>Family</span><span>${a.font_family}</span>` : null}
          ${a.tris ? html`<span>Triangles</span><span>${a.tris.toLocaleString()}</span>` : null}
          ${a.clips ? html`<span>Animation</span><span>${plural(a.clips, 'clip')}${a.rigged ? ', rigged' : ''}</span>` : a.rigged ? html`<span>Rig</span><span>skinned</span>` : null}
          ${a.missing ? html`<span>Missing</span><span class="err" title="Files the model loads that aren't in the library">${plural(a.missing, 'file')}</span>` : null}
          <span>License</span><span title=${a.license_info?.summary || ''}>${a.license_info?.name || a.license}</span>
          <span>AI</span><span>${a.ai_generated}${a.ai_reason ? html` <span class="faint">· ${a.ai_reason}</span>` : ''}</span>
          <span>Position</span><span>${i + 1} of ${v.total.toLocaleString()}</span>
        </div>
        <section>
          <div class="dim" style="font-size:12px">Files</div>
          ${a.files.map((f) => html`<div class="row mono" style="font-size:11px"><a href=${libUrl(f.path)} target="_blank" class="grow" style="word-break:break-all">${f.path.slice(a.pack_path.length + 1)}</a><span class="faint">${bytes(f.size)}</span></div>`)}
          <div class="row">
            <button onClick=${() => api('/api/reveal', { path: a.path }).catch(fail)}>Reveal</button>
            <button onClick=${() => navigator.clipboard.writeText(a.abs_path).then(() => toast('Path copied'))}>Copy path</button>
            ${(a.media === 'visual' || a.preview) && state.meta?.embed?.ready ? html`<button title="Images that look like this one, across the library"
              onClick=${() => { close(); go('assets', { similar: a.id }); }}>More like this</button>` : null}
            ${a.media === 'visual' && a.displayable ? html`<button title="Make this the pack's cover image"
              onClick=${() => api('/api/edit/pack', { id: a.pack_id, fields: { cover: inner } }).then(() => toast('Cover saved to curation'), fail)}>Use as cover</button>` : null}
          </div>
        </section>
        ${a.media === 'visual' && W ? html`<section>
          <div class="row"><span class="dim grow" style="font-size:12px">Grid ${a.grid_source ? html`<span class="faint">(from ${a.grid_source})</span>` : ''}</span></div>
          <div class="row" style="font-size:12px">
            <label>w <input type="number" min="1" value=${gEdit.w} onInput=${(e) => setGv('w', e.target.value)} /></label>
            <label>h <input type="number" min="1" value=${gEdit.h} onInput=${(e) => setGv('h', e.target.value)} /></label>
            <label>gap <input type="number" min="0" value=${gEdit.spacing} onInput=${(e) => setGv('spacing', e.target.value)} /></label>
            <label>margin <input type="number" min="0" value=${gEdit.margin} onInput=${(e) => setGv('margin', e.target.value)} /></label>
          </div>
          <div class="row">${[8, 16, 24, 32, 48, 64, 128].map((n) => html`<button class="chip" onClick=${() => setG({ w: n, h: n, spacing: 0, margin: 0 })}>${n}</button>`)}</div>
          ${g && !fits ? html`<div class="err" style="font-size:12px">Doesn't tile ${W}×${H} evenly.</div>` : null}
          ${fits ? html`<div class="faint" style="font-size:12px">${fits.cols} × ${fits.rows} cells</div>` : null}
          <div class="row">
            <button class="primary" disabled=${!fits} onClick=${() => saveGrid(g)}>Save grid</button>
            ${a.grid_source === 'curation' ? html`<button onClick=${() => saveGrid(null)}>Remove</button>` : null}
          </div>
        </section>
        <section>
          <div class="dim" style="font-size:12px">Animate</div>
          ${g ? html`<${Animator} src=${src} W=${W} H=${H} g=${g} />` : html`<div class="faint" style="font-size:12px">Set a grid above to play rows as animations.</div>`}
        </section>` : null}
        <div class="faint" style="font-size:11px">${a.media === 'model' ? '←/→ previous/next · drag to turn, scroll to zoom, right-drag to pan · Esc close'
          : '←/→ previous/next · 0 fit · 1–8 zoom · g grid · Esc close'}</div>
      </div>
    </div>
  </div>`;
}

// --- pack page --------------------------------------------------------------------------------

function buildTree(folders) {
  const root = { name: '', path: '', count: 0, kids: [] };
  const byPath = { '': root };
  for (const f of folders) {
    const parts = f.folder.split('/');
    const node = { name: parts[parts.length - 1], path: f.folder, count: f.count, kids: [] };
    byPath[f.folder] = node;
    (byPath[parts.slice(0, -1).join('/')] || root).kids.push(node);
  }
  return root;
}

function Tree({ node, sel, open, toggle, pick, depth = 0 }) {
  return node.kids.map((n) => {
    const isOpen = open.has(n.path);
    return html`<div key=${n.path}>
      <div class="node ${sel === n.path ? 'on' : ''}" style=${`padding-left:${4 + depth * 12}px`} onClick=${() => pick(n.path)}>
        <span class="tw" onClick=${(e) => { e.stopPropagation(); toggle(n.path); }}>${n.kids.length ? (isOpen ? '▾' : '▸') : ''}</span>
        <span class="nm">${n.name}</span><span class="ct">${n.count}</span>
      </div>
      ${isOpen ? html`<${Tree} node=${n} sel=${sel} open=${open} toggle=${toggle} pick=${pick} depth=${depth + 1} />` : null}
    </div>`;
  });
}

function PackEditor({ pack, onSaved }) {
  const m = state.meta;
  const cv = pack.curation_values || {};
  const [f, setF] = useState({
    license: cv.license || '', ai_generated: cv.ai_generated || '', ai_reason: cv.ai_reason || '',
    tags: (cv.tags || []).join(', '), notes: cv.notes || '', description: cv.description || '',
  });
  const gc = pack.group_curation || {};
  const [gf, setGf] = useState({ store: gc.store || '', purchased: gc.purchased || '', license: gc.license || '', ai_generated: gc.ai_generated || '', ai_reason: gc.ai_reason || '', notes: gc.notes || '' });
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const d = await api('/api/edit/pack', { id: pack.id, fields: { ...f, tags: f.tags.split(',').map((t) => t.trim()).filter(Boolean) } });
      toast('Saved to ' + pack.curation.file); onSaved(d);
    } catch (e) { fail(e); }
    setBusy(false);
  };
  const saveGroup = async () => {
    setBusy(true);
    try {
      await api('/api/edit/group', { id: pack.group.id, fields: { ...gf, purchased: gf.purchased ? +gf.purchased : null } });
      toast('Saved bundle defaults'); onSaved(await api(`/api/pack?id=${enc(pack.id)}`)); loadMeta();
    } catch (e) { fail(e); }
    setBusy(false);
  };
  const inp = (st, set, k, ph = '') => html`<input value=${st[k]} placeholder=${ph} onInput=${(e) => set({ ...st, [k]: e.target.value })} />`;
  const licSel = (st, set, inheritLabel) => html`<select value=${st.license} onChange=${(e) => set({ ...st, license: e.target.value })}>
    <option value="">${inheritLabel}</option>${Object.values(m.licenses).map((l) => html`<option value=${l.key}>${l.name} (${l.key})</option>`)}</select>`;
  const aiSel = (st, set, inheritLabel) => html`<select value=${st.ai_generated} onChange=${(e) => set({ ...st, ai_generated: e.target.value })}>
    <option value="">${inheritLabel}</option><option value="no">no</option><option value="unknown">unknown</option><option value="yes">yes</option></select>`;
  return html`<div class="panel" style="display:flex;flex-direction:column;gap:14px">
    <div class="editor">
      <h3 style="grid-column:1/3">This pack <span class="faint mono" style="font-weight:400">${pack.curation.file} ${pack.curation.key}</span></h3>
      <label>License</label>${licSel(f, setF, cv.license ? 'inherit' : `inherit (${pack.license})`)}
      <label>AI-generated</label>${aiSel(f, setF, cv.ai_generated ? 'inherit' : `inherit (${pack.ai_generated})`)}
      <label>AI reason</label>${inp(f, setF, 'ai_reason', 'why we believe that')}
      <label>Your tags</label>${inp(f, setF, 'tags', 'pixel-art, top-down, dungeon')}
      <label>Description</label><textarea value=${f.description} onInput=${(e) => setF({ ...f, description: e.target.value })}></textarea>
      <label>Notes</label><textarea value=${f.notes} onInput=${(e) => setF({ ...f, notes: e.target.value })}></textarea>
      <span></span><div class="row"><button class="primary" disabled=${busy} onClick=${save}>Save pack</button>
        <span class="faint" style="font-size:12px">Empty fields inherit. Sub-packs inherit too.</span></div>
    </div>
    <details>
      <summary class="dim">Bundle defaults: ${pack.group.name}</summary>
      <div class="editor" style="margin-top:10px">
        <label>Store</label>${inp(gf, setGf, 'store')}
        <label>Purchased</label>${inp(gf, setGf, 'purchased', 'year')}
        <label>License</label>${licSel(gf, setGf, 'unknown')}
        <label>AI-generated</label>${aiSel(gf, setGf, 'unknown')}
        <label>AI reason</label>${inp(gf, setGf, 'ai_reason')}
        <label>Notes</label>${inp(gf, setGf, 'notes')}
        <span></span><div><button class="primary" disabled=${busy} onClick=${saveGroup}>Save bundle defaults</button></div>
      </div>
    </details>
  </div>`;
}

function PackPage({ id, params }) {
  const p = paramsObj(params);
  const [pk, setPk] = useState(null);
  const [folders, setFolders] = useState([]);
  const [open, setOpen] = useState(new Set());
  const [editing, setEditing] = useState(false);
  const [count, setCount] = useState(null);
  const [size, setSize] = useState(prefs.tile || 128);
  const [bg, setBg] = useState(prefs.bg || 'checker');
  const folder = p.folder || '';
  const layout = p.layout || 'grid';
  useEffect(() => {
    setPk(null); setFolders([]); setEditing(false);
    api(`/api/pack?id=${enc(id)}`).then(setPk, fail);
    api(`/api/folders?pack=${enc(id)}`).then((fs) => {
      setFolders(fs);
      const o = new Set(fs.filter((f) => !f.folder.includes('/')).length <= 6 ? fs.filter((f) => !f.folder.includes('/')).map((f) => f.folder) : []);
      folder.split('/').forEach((_, i, a) => o.add(a.slice(0, i + 1).join('/')));
      setOpen(o);
    }, fail);
    scrollTo(0, 0);
  }, [id]);
  const tree = useMemo(() => buildTree(folders), [folders]);
  if (!pk) return html`<div class="empty">Loading…</div>`;
  const set = (k, v) => go('pack/' + id, { ...p, [k]: v }, true);
  const types = Object.entries(pk.type_counts);
  const media = p.media || '';
  const gridParams = { pack: id, folder, type: p.type || '', media, q: p.q || '' };
  const sheet = async () => {
    try { const d = await api('/api/sheet', { pack: id, folder }); window.open(d.url, '_blank'); } catch (e) { fail(e); }
  };
  const audioCount = (pk.type_counts.music || 0) + (pk.type_counts.sfx || 0) + (pk.type_counts.voice || 0);
  const crumbs = [{ id: null, name: pk.group.name, href: `#/packs?group=${enc(pk.group.id)}` }, ...pk.breadcrumbs.map((b) => ({ ...b, href: '#/pack/' + b.id }))];
  return html`<div>
    <div class="crumbs">${crumbs.map((c) => html`<a href=${c.href}>${c.name}</a> › `)}<span>${pk.name}</span></div>
    <div class="packhead">
      ${pk.cover_url || pk.sheet_url
        ? html`<div class="cover" onClick=${() => window.open(pk.cover_kind === 'bundled' ? libUrl(pk.cover) : pk.sheet_url, '_blank')}>
            <img src=${pk.cover_url || pk.sheet_url} onLoad=${(e) => { const i = e.target; if (i.naturalWidth * 1.5 < i.clientWidth) i.classList.add('px'); }} /></div>`
        : html`<div class="cover"><span class="glyph">${GLYPH[pk.kind]}</span></div>`}
      <div>
        <div class="row"><h1 class="grow">${pk.name}</h1><${Star} kind="pack" refv=${pk.id} /></div>
        <div class="dim">${pk.group.name}${pk.group.store ? ` · ${pk.group.store}` : ''}${pk.group.purchased ? `, bought ${pk.group.purchased}` : ''}</div>
        <div class="badges row" style="margin-top:8px">
          <span class="badge kind">${GLYPH[pk.kind]} ${pk.kind}</span>${pk.role === 'collection' ? html`<span class="badge">collection of ${pk.sub_packs.length}</span>` : null}
          ${licBadge(pk.license)} ${aiBadge(pk.ai_generated)}
        </div>
        <${PackTags} pk=${pk} onChange=${setPk} />
        ${pk.description ? html`<p>${pk.description}${pk.description_source === 'ai' ? html` <span class="faint" style="font-size:11px">(AI)</span>` : null}</p>` : null}
        <dl class="facts">
          <dt>Contents</dt><dd>${plural(pk.asset_count, 'asset')} in ${plural(pk.file_count, 'file')}, ${bytes(pk.total_size)}${pk.pending ? html` · <span class="err">${pk.pending} not synced</span>` : ''}</dd>
          <dt>Types</dt><dd>${types.map(([t, n]) => `${n} ${t}`).join(', ')}</dd>
          ${pk.contains && Object.keys(pk.contains).length ? html`<dt title="What the images show, as recognized by the image model. Click one to see those images.">Contains</dt>
            <dd class="chips">${Object.entries(pk.contains).map(([k, n]) => { const w = k.replace(/-/g, ' '); return html`<span class="chip subj ${p.q === w ? 'on' : ''}"
              onClick=${() => { set('q', p.q === w ? '' : w); if (p.q !== w) setTimeout(() => document.getElementById('files')?.scrollIntoView({ behavior: 'smooth' }), 50); }}>${w}<small>${n}</small></span>`; })}</dd>` : null}
          ${pk.engines ? html`<dt>Engines</dt><dd>${pk.engines}</dd>` : null}
          <dt>License</dt><dd>${pk.license_info.name || pk.license} <span class="faint">(from ${pk.license_from})</span>
            ${pk.license_info.url ? html` · <a href=${pk.license_info.url} target="_blank">terms</a>` : null}
            ${pk.license_file ? html` · <a href=${libUrl(pk.license_file)} target="_blank">license file</a>` : null}
            ${pk.license_info.summary ? html`<div class="faint">${pk.license_info.summary}${pk.license_info.verified === false ? ' (summary not verified)' : ''}</div>` : null}</dd>
          <dt>AI status</dt><dd>${pk.ai_generated}${pk.ai_reason ? html` <span class="faint">· ${pk.ai_reason}</span>` : null}</dd>
          ${pk.notes ? html`<dt>Notes</dt><dd>${pk.notes}</dd>` : null}
          ${pk.duplicates.length ? html`<dt>Shares files</dt><dd>${pk.duplicates.slice(0, 5).map((d) => html`<div><a href=${'#/pack/' + d.other}>${d.other.split('/').pop()}</a> <span class="faint">${d.shared_files} files, ${Math.round(d.share_of_this * 100)}% of this pack</span></div>`)}</dd>` : null}
          <dt>Folder</dt><dd class="mono" style="word-break:break-all">${pk.path}</dd>
        </dl>
        <div class="actions">
          <button onClick=${() => setEditing(!editing)}>${editing ? 'Close editor' : 'Edit curation'}</button>
          <button onClick=${() => api('/api/reveal', { path: pk.path }).catch(fail)}>Reveal in ${navigator.platform.startsWith('Mac') ? 'Finder' : 'Explorer'}</button>
          <button onClick=${sheet}>Contact sheet</button>
          <a class="btnlink" href=${`#/layout/${pk.group.id}?focus=${enc(pk.id.slice(pk.group.id.length + 1))}`} title="Pack, collection, grouping or ignore">Change pack tree</a>
          ${audioCount ? html`<button onClick=${() => set('media', 'audio')}>♪ Audio only (${audioCount})</button>` : null}
        </div>
      </div>
    </div>
    ${editing ? html`<div style="margin-top:16px"><${PackEditor} pack=${pk} onSaved=${(d) => { setPk(d); loadMeta(); }} /></div>` : null}
    ${pk.previews.length ? html`<h2>Previews</h2><div class="strip">${pk.previews.map((x) => html`<div class="shot bg-dark" onClick=${() => window.open(libUrl(x.path), '_blank')} title=${x.inner}>
        <img src=${x.width && x.width <= 600 ? libUrl(x.path) : thumbUrl(x.path, 384)} loading="lazy" onLoad=${(e) => { const i = e.target; if (i.naturalHeight < 150) i.classList.add('px'); }} /></div>`)}</div>` : null}
    ${pk.sub_packs.length ? html`<h2>${plural(pk.sub_packs.length, 'sub-pack')}</h2><div class="cards">${pk.sub_packs.map((c) => html`<${PackCard} key=${c.id} c=${c} />`)}</div>` : null}
    ${pk.docs.length ? html`<h2>Readme and license files</h2>${pk.docs.map((d) => html`<details class="doc"><summary>${d.role}: <span class="mono">${d.path.slice(pk.path.length + 1) || d.path}</span></summary><pre>${d.text}</pre></details>`)}` : null}
    <h2 id="files">Files</h2>
    <div class="browser">
      <div class="tree">
        <div class="node ${folder === '' ? 'on' : ''}" onClick=${() => set('folder', '')}><span class="tw"></span><span class="nm">All files</span><span class="ct">${pk.asset_count}</span></div>
        <${Tree} node=${tree} sel=${folder} open=${open} pick=${(f) => set('folder', f)}
          toggle=${(f) => { const o = new Set(open); o.has(f) ? o.delete(f) : o.add(f); setOpen(o); }} />
      </div>
      <div>
        <div class="toolbar">
          <input type="search" style="min-width:250px" placeholder="Filter by name or what it shows…" value=${p.q || ''} onChange=${(e) => set('q', e.target.value)} />
          ${p.q || p.type || p.media ? html`<button class="clearbtn" title="Show all files again" onClick=${() => go('pack/' + id, { ...p, q: '', type: '', media: '' }, true)}>✕ Clear filter</button>` : null}
          <select value=${p.type || ''} onChange=${(e) => set('type', e.target.value)}>
            <option value="">All types</option>${types.map(([t, n]) => html`<option value=${t}>${t} (${n})</option>`)}
          </select>
          <select value=${media} onChange=${(e) => set('media', e.target.value)}>
            <option value="">All media</option><option value="visual">Images</option><option value="audio">Audio</option><option value="font">Fonts</option><option value="model">3D</option>
          </select>
          <select value=${layout} onChange=${(e) => set('layout', e.target.value)}><option value="grid">Grid</option><option value="list">List</option></select>
          ${layout === 'grid' ? html`<select value=${size} onChange=${(e) => { setSize(+e.target.value); savePref('tile', +e.target.value); }}>
            <option value="96">Small</option><option value="128">Medium</option><option value="192">Large</option><option value="288">Huge</option>
          </select>` : null}
          <select value=${bg} onChange=${(e) => { setBg(e.target.value); savePref('bg', e.target.value); }}>
            <option value="checker">Checker</option><option value="dark">Dark</option><option value="light">Light</option>
          </select>
          ${folder ? html`<${Star} kind="folder" refv=${pk.path + '/' + folder} />` : null}
          <span class="dim">${count != null ? plural(count, 'asset') : ''}</span>
        </div>
        <${AssetGrid} params=${gridParams} size=${size} bg=${bg} onTotal=${setCount} list=${layout === 'list'} />
      </div>
    </div>
  </div>`;
}

// --- asset search -----------------------------------------------------------------------------

function AssetsPage({ params }) {
  const s = useStore();
  const p = paramsObj(params);
  const [count, setCount] = useState(null);
  const [size, setSize] = useState(prefs.tile || 128);
  const set = (k, v) => go('assets', { ...p, [k]: v }, true);
  const m = s.meta;
  if (!m) return null;
  const searching = p.q || p.type || p.media || p.group || p.grid || p.similar || p.animated || p.rigged;
  const byMeaning = m.embed?.ready && p.meaning === '1';
  return html`<div>
    ${p.similar ? html`<${SimilarHead} id=${p.similar} onClear=${() => set('similar', '')} />` : null}
    <div class="toolbar">
      <input type="search" style="min-width:260px" value=${p.q || ''} onChange=${(e) => go('assets', { ...p, q: e.target.value, similar: '' }, true)}
        placeholder=${byMeaning ? 'Describe it: a slime monster, a wooden door, fire…' : 'Search all assets: sword, coin, explosion…'} />
      ${m.embed?.ready ? html`<${Chips} options=${['words', 'meaning']} value=${byMeaning ? 'meaning' : 'words'}
        onChange=${(v) => set('meaning', v === 'meaning' ? '1' : '')} />` : null}
      <select value=${p.media || ''} onChange=${(e) => set('media', e.target.value)}>
        <option value="">All media</option><option value="visual">Images</option><option value="audio">Audio</option><option value="font">Fonts</option><option value="model">3D</option>
      </select>
      <select value=${p.type || ''} onChange=${(e) => set('type', e.target.value)}>
        <option value="">All types</option>${Object.entries(m.types).map(([t, n]) => html`<option value=${t}>${t} (${n.toLocaleString()})</option>`)}
      </select>
      <select value=${p.group || ''} onChange=${(e) => set('group', e.target.value)}>
        <option value="">All bundles</option>${m.groups.map((g) => html`<option value=${g.id}>${g.name}</option>`)}
      </select>
      <select value=${p.ai || ''} onChange=${(e) => set('ai', e.target.value)}>
        <option value="">Any AI status</option><option value="no">human-made</option><option value="unknown">unknown</option>
      </select>
      ${p.media === 'model' ? html`
        <label class="row dim" title="glTF models with animation clips"><input type="checkbox" checked=${!!p.animated} onChange=${(e) => set('animated', e.target.checked)} /> animated</label>
        <label class="row dim" title="glTF models with a skeleton"><input type="checkbox" checked=${!!p.rigged} onChange=${(e) => set('rigged', e.target.checked)} /> rigged</label>`
      : html`<label class="row dim"><input type="checkbox" checked=${!!p.grid} onChange=${(e) => set('grid', e.target.checked)} /> has grid</label>`}
      <select value=${p.layout || 'grid'} onChange=${(e) => set('layout', e.target.value)}><option value="grid">Grid</option><option value="list">List</option></select>
      <select value=${size} onChange=${(e) => { setSize(+e.target.value); savePref('tile', +e.target.value); }}>
        <option value="96">Small</option><option value="128">Medium</option><option value="192">Large</option>
      </select>
      <span class="dim">${searching && count != null ? plural(count, 'asset') : ''}</span>
    </div>
    ${searching
      ? html`<${AssetGrid} params=${{ q: p.q || '', type: p.type || '', media: p.media || '', group: p.group || '', ai: p.ai || '', grid: !!p.grid,
            animated: p.media === 'model' && !!p.animated, rigged: p.media === 'model' && !!p.rigged,
            meaning: byMeaning && !!p.q, similar: p.similar || '' }}
          size=${size} bg=${prefs.bg || 'checker'} showPack=${true} onTotal=${setCount} list=${p.layout === 'list'} />`
      : html`<div class="empty">Search every asset in the library by name, folder, pack or type${m.embed?.ready
          ? html`, or switch to <b>meaning</b> and describe what you're looking for` : ''}.<br/>Audio tiles play on click; everything else opens the viewer.</div>`}
  </div>`;
}

function SimilarHead({ id, onClear }) {
  const [a, setA] = useState(null);
  useEffect(() => { api(`/api/asset?id=${enc(id)}`).then(setA, fail); }, [id]);
  return html`<div class="row" style="margin-bottom:10px;gap:10px">
    ${a ? html`<img src=${thumbUrl(a.path, 48)} style="width:48px;height:48px;object-fit:contain;image-rendering:pixelated;background:var(--tile-bg);border-radius:4px" />` : null}
    <div class="grow"><div class="dim" style="font-size:12px">Looks like</div><div>${a ? html`${a.name} <span class="faint">· ${a.pack_name}</span>` : '…'}</div></div>
    <button onClick=${onClear}>Clear</button>
  </div>`;
}

// --- shortlists -------------------------------------------------------------------------------

function ShortlistsPage({ name }) {
  const s = useStore();
  const [d, setD] = useState(null);
  const [newName, setNewName] = useState('');
  useEffect(() => { if (name) api(`/api/shortlist?name=${enc(name)}`).then(setD, fail); else setD(null); }, [name, s.marks]);
  const lists = s.meta?.shortlists || [];
  const create = async () => {
    const n = newName.trim().toLowerCase().replace(/\s+/g, '-');
    try { await api('/api/shortlist', { name: n }); setNewName(''); await loadMeta(); go('shortlists/' + n); } catch (e) { fail(e); }
  };
  const items = d?.items || [];
  const get = async (i) => items.filter((x) => x.asset)[i]?.asset;
  const assets = items.filter((x) => x.kind === 'asset');
  return html`<div style="display:grid;grid-template-columns:220px 1fr;gap:18px;align-items:start">
    <aside class="facets">
      <label class="h">Shortlists</label>
      ${lists.map((l) => html`<div class="row"><a class="grow" href=${'#/shortlists/' + l.name} style=${l.name === name ? 'color:var(--text);font-weight:600' : ''}>${l.name}</a>
        <span class="faint">${l.count}</span></div>`)}
      <div class="row"><input class="grow" placeholder="new-list-name" value=${newName} onInput=${(e) => setNewName(e.target.value)} onKeyDown=${(e) => e.key === 'Enter' && create()} /><button onClick=${create}>Add</button></div>
      <div class="faint" style="font-size:12px">Saved in the profile's <span class="mono">shortlists/</span> folder. In a game session, ask Claude to use one.</div>
    </aside>
    <section>
      ${!name ? html`<div class="empty">${lists.length ? 'Pick a shortlist.' : 'No shortlists yet. Click the ☆ on any pack, folder or asset to start one.'}</div>` : null}
      ${name && d ? html`<div>
        <div class="row"><h1 class="grow">${name}</h1></div>
        ${d.notes ? html`<p class="dim">${d.notes}</p>` : null}
        ${!items.length ? html`<div class="empty">Empty. Click the ☆ on a pack, folder or asset while browsing and tick this list.</div>` : null}
        ${items.some((x) => x.kind === 'pack') ? html`<h2>Packs</h2><div class="cards">${items.filter((x) => x.kind === 'pack').map((x) => x.pack ? html`<${PackCard} c=${x.pack} />` : html`<div class="panel err">Missing: ${x.ref}</div>`)}</div>` : null}
        ${items.some((x) => x.kind === 'folder') ? html`<h2>Folders</h2><div class="list-items">${items.filter((x) => x.kind === 'folder').map((x) => html`<div class="li">
            ${x.pack ? html`<a class="grow" href=${`#/pack/${x.pack.id}?folder=${enc(x.pack.folder)}`}>${x.pack.name} / ${x.pack.folder}</a>` : html`<span class="grow err">Missing: ${x.ref}</span>`}
            <${Star} kind="folder" refv=${x.ref} /></div>`)}</div>` : null}
        ${assets.length ? html`<h2>Assets</h2><div class="list-items">${assets.map((x, i) => html`<div class="li">
            ${x.asset ? html`<div class="thumb64 bg-checker">${x.asset.media === 'visual' ? html`<${PxImg} src=${thumbUrl(x.asset.path, 64)} w=${x.asset.width} h=${x.asset.height} palette=${x.asset.palette} boxW=${64} boxH=${64} />` : html`<span class="dim">${x.asset.media === 'audio' ? '♪' : x.asset.ext}</span>`}</div>
              <div class="grow"><a href="javascript:void 0" onClick=${() => (x.asset.media === 'audio' ? playAt(get, items.filter((y) => y.asset).indexOf(x), assets.length) : setState({ viewer: { get, i: items.filter((y) => y.asset).indexOf(x), total: items.filter((y) => y.asset).length } }))}>${x.asset.name}</a>
                <div class="faint mono" style="font-size:11px">${x.asset.pack_name} / ${x.asset.inner}</div></div>`
              : html`<span class="grow err">Missing: ${x.ref}</span>`}
            <${Star} kind="asset" refv=${x.ref} /></div>`)}</div>` : null}
      </div>` : null}
    </section>
  </div>`;
}

// --- layout: pack roles -----------------------------------------------------------------------

const ROLE_HELP = {
  grouping: 'just a folder: its sub-folders are looked at for packs',
  pack: 'one pack; everything inside belongs to it',
  collection: 'a pack whose sub-folders are packs too',
  ignore: 'not indexed at all',
};

function LayoutRow({ n, depth, open, toggle, setRole, focus, saved, stale }) {
  const isOpen = open(n);
  const changed = n.role !== 'content' && (n.set || '') !== (saved[n.key] || '');
  const opts = n.ctx === 'collection' ? ['pack', 'collection', 'ignore'] : ['grouping', 'pack', 'collection', 'ignore'];
  const ref = useRef();
  useEffect(() => { if (focus === n.key) ref.current?.scrollIntoView({ block: 'center' }); }, [focus]);
  return html`<div>
    <div ref=${ref} class="lnode role-${n.role} ${focus === n.key ? 'focus' : ''}" style=${`padding-left:${4 + depth * 16}px`}>
      <span class="tw" onClick=${() => n.kids.length && toggle(n.key, n)}>${n.kids.length ? (isOpen ? '▾' : '▸') : ''}</span>
      <span class="nm" title=${n.key}>${n.name}</span>
      ${n.more ? html`<span class="faint" style="font-size:11px">${plural(n.more, 'folder')}</span>` : null}
      <span class="ct">${plural(n.files, 'file')}</span>
      ${n.role === 'content'
        ? html`<span class="rolesel faint">in pack</span>`
        : html`<span class="rolesel">
            ${changed ? html`<span class="changed" title="Not saved yet">●</span>` : null}
            ${n.pack && !changed && !stale ? html`<a href=${'#/pack/' + n.pack} title="Open the pack">↗</a>` : null}
            <select value=${n.set || ''} class=${n.set ? 'set' : ''} onChange=${(e) => setRole(n.key, e.target.value || null)}
              title=${ROLE_HELP[n.role] || ''}>
              <option value="">auto: ${n.auto}</option>
              ${opts.map((o) => html`<option value=${o}>${o}</option>`)}
            </select></span>`}
    </div>
    ${isOpen ? n.kids.map((k) => html`<${LayoutRow} key=${k.key} n=${k} depth=${depth + 1} open=${open} toggle=${toggle}
      setRole=${setRole} focus=${focus} saved=${saved} stale=${stale} />`) : null}
  </div>`;
}

function LayoutPage({ gid, params }) {
  const s = useStore();
  const focus = params.get('focus') || '';
  const [v, setV] = useState(null);
  const [pending, setPending] = useState({});
  const [saved, setSaved] = useState({});
  const [toggled, setToggled] = useState({});  // key -> open, where the user chose
  const [busy, setBusy] = useState('');
  const savedRoles = (tree) => {
    const out = {};
    const walk = (ns) => ns.forEach((n) => { if (n.set) out[n.key] = n.set; walk(n.kids); });
    walk(tree);
    return out;
  };
  const load = async (roles = {}) => {
    try {
      const d = await api('/api/layout', { group: gid, roles });
      setV(d);
      return d;
    } catch (e) { fail(e); }
  };
  useEffect(() => {
    setV(null); setPending({}); setToggled({});
    if (gid) load().then((d) => d && setSaved(savedRoles(d.tree)));
  }, [gid]);
  const m = s.meta;
  const groups = m?.groups || [];
  const setRole = (key, role) => {
    const p = { ...pending, [key]: role };
    if ((role || '') === (saved[key] || '')) delete p[key];
    setPending(p);
    load(p);
  };
  // Only send roles for folders that still take one in the preview (a parent may have changed).
  const live = () => {
    const keys = new Set();
    const walk = (ns) => ns.forEach((n) => { if (n.role !== 'content') keys.add(n.key); walk(n.kids); });
    walk(v.tree);
    return Object.fromEntries(Object.entries(pending).filter(([k, r]) => r === null || keys.has(k)));
  };
  const save = async () => {
    setBusy('save');
    try {
      const d = await api('/api/edit/roles', { group: gid, roles: live() });
      setV(d); setSaved(savedRoles(d.tree)); setPending({});
      toast('Saved to ' + d.group.file);
    } catch (e) { fail(e); }
    setBusy('');
  };
  const apply = async () => {
    setBusy('apply');
    try {
      const r = await api('/api/reindex', {});
      toast(`Re-indexed: ${plural(r.packs, 'pack')} in ${r.seconds}s`);
      await loadMeta(); await load(pending);
    } catch (e) { fail(e); }
    setBusy('');
  };
  const defaultOpen = (n) => n.role === 'grouping' || n.role === 'collection' || (!!focus && focus.startsWith(n.key + '/'));
  const open = (n) => toggled[n.key] ?? defaultOpen(n);
  const toggle = (key, n) => setToggled({ ...toggled, [key]: !open(n) });
  const nPending = Object.keys(pending).length;
  const sum = (c) => `${plural(c.packs, 'pack')}${c.collections ? `, ${plural(c.collections, 'collection')}` : ''}`;
  const sections = [...new Set(groups.map((g) => g.section))];
  return html`<div class="packs">
    <aside class="facets">
      ${sections.map((sec) => html`<div><label class="h">${sec}</label>
        ${groups.filter((g) => g.section === sec).map((g) => html`<div class="row">
          <a class="grow" href=${'#/layout/' + g.id} style=${g.id === gid ? 'color:var(--text);font-weight:600' : ''}>${g.name}</a>
          <span class="faint">${g.packs}</span></div>`)}</div>`)}
    </aside>
    <section>
      ${!gid ? html`<div class="panel layouthelp">
          <h1>Layout</h1>
          <p>How folders become packs. AssetCat guesses each folder's role; when it guesses wrong, pick a bundle or store on the left and set the role by hand.</p>
          <dl class="facts">${Object.entries(ROLE_HELP).map(([r, t]) => html`<dt>${r}</dt><dd>${t}</dd>`)}</dl>
          <p class="faint">Roles are saved in the group's curation file in the profile and take effect after a re-index, which this page can run.</p>
        </div>` : null}
      ${gid && !v ? html`<div class="empty">Loading…</div>` : null}
      ${gid && v ? html`<div>
        <div class="row" style="margin-bottom:6px"><h1 class="grow">${v.group.name}</h1>
          <a href=${'#/packs?group=' + enc(gid)}>Packs →</a></div>
        <div class="dim">${sum(v.summary)}${nPending ? html` <span class="faint">with your changes (now ${sum(v.indexed)})</span>` : null}
          ${' · '}<span class="mono">${v.group.file}</span></div>
        ${nPending ? html`<div class="panel row bar">
            <span class="grow">${plural(nPending, 'unsaved change')}. The tree below is the preview.</span>
            <button onClick=${() => { setPending({}); load(); }} disabled=${!!busy}>Discard</button>
            <button class="primary" onClick=${save} disabled=${!!busy}>${busy === 'save' ? 'Saving…' : 'Save'}</button></div>`
          : v.stale ? html`<div class="panel row bar">
            <span class="grow">Saved, but the index still has ${sum(v.indexed)}. Re-index to apply (takes a while on a big library).</span>
            <button class="primary" onClick=${apply} disabled=${!!busy}>${busy === 'apply' ? 'Re-indexing…' : 'Re-index now'}</button></div>`
          : null}
        <div class="ltree panel">
          ${v.tree.map((n) => html`<${LayoutRow} key=${n.key} n=${n} depth=${0} open=${open} toggle=${toggle} setRole=${setRole} focus=${focus} saved=${saved} stale=${v.stale} />`)}
        </div>
        <div class="faint" style="font-size:12px;margin-top:8px">${Object.entries(ROLE_HELP).map(([r, t]) => html`<div><b>${r}</b>: ${t}</div>`)}</div>
      </div>` : null}
    </section>
  </div>`;
}

// --- app shell --------------------------------------------------------------------------------

function App() {
  const s = useStore();
  const r = useRoute();
  const [mode, setMode] = useState('packs');
  useEffect(() => { loadMeta().catch(fail); loadMarks(); }, []);
  useEffect(() => { if (r.route === 'assets') setMode('assets'); else if (r.route === 'packs') setMode('packs'); }, [r.route]);
  const search = (e) => {
    e.preventDefault();
    const q = e.target.q.value.trim();
    go(mode, { q });
  };
  let page;
  if (r.route === 'pack') page = html`<${PackPage} id=${r.rest} params=${r.params} />`;
  else if (r.route === 'assets') page = html`<${AssetsPage} params=${r.params} />`;
  else if (r.route === 'shortlists') page = html`<${ShortlistsPage} name=${r.rest} />`;
  else if (r.route === 'layout') page = html`<${LayoutPage} gid=${r.rest} params=${r.params} />`;
  else page = html`<${PacksPage} params=${r.params} />`;
  const qv = ['packs', 'assets'].includes(r.route) ? r.params.get('q') || '' : '';
  return html`<div>
    <header class="top">
      <a class="brand" href="#/packs"><img src="/web/logo.svg" width="26" height="26" alt="" />AssetCat</a>
      <nav class="nav">
        <a class=${r.route === 'packs' || r.route === 'pack' ? 'on' : ''} href="#/packs">Packs</a>
        <a class=${r.route === 'assets' ? 'on' : ''} href="#/assets">Assets</a>
        <a class=${r.route === 'shortlists' ? 'on' : ''} href="#/shortlists/">Shortlists</a>
        <a class=${r.route === 'layout' ? 'on' : ''} href="#/layout">Layout</a>
      </nav>
      <form class="search row" onSubmit=${search}>
        <select value=${mode} onChange=${(e) => setMode(e.target.value)}><option value="packs">Packs</option><option value="assets">Assets</option></select>
        <input name="q" class="grow" type="search" placeholder=${mode === 'packs' ? 'Search packs…' : 'Search assets…'} value=${qv} key=${r.route + qv} />
      </form>
    </header>
    <main>${s.meta ? page : html`<div class="empty">Loading…</div>`}</main>
    ${s.viewer ? html`<${Viewer} />` : null}
    <${Player} />
    <${StarMenu} />
    ${s.toast ? html`<div class="toast ${s.toast.err ? 'err' : ''}">${s.toast.msg}</div>` : null}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
