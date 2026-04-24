import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import TennisEngine from './engine.js';

const SIGNAL_TOOLTIPS = {
  NMI: 'Near-Miss Index — break point pressure created even without converting',
  SMS: 'Serve Module — first/second serve win %, speed trend, hold efficiency',
  RMS: 'Return Module — return points won, break conversion, near-miss pressure',
  PMS: 'Physical Module — rally length bucket dominance (short/medium/long)',
  GPS: 'Game Length Pressure — how long service games are lasting',
};

const RALLY_OPTIONS = [1, 2, 3, 4, 5, 6, 8, 10, '15+'];
const SPEED_OPTIONS = [
  { label: '0.25×', ms: 4000 },
  { label: '0.5×',  ms: 2000 },
  { label: '1×',    ms: 1000 },
  { label: '2×',    ms: 500  },
  { label: '5×',    ms: 200  },
  { label: 'Max',   ms: 50   },
];

// ═══════════════════════════════════════════════════════════
// NAV
// ═══════════════════════════════════════════════════════════

function Nav({ tab, onTab, isLive }) {
  return (
    <nav className="nav">
      <div className="wordmark">
        <span className="emblem">T</span>
        Tennis Engine
      </div>
      <div className="nav-tabs">
        {['Live', 'History', 'Models'].map(t => (
          <button key={t} className={`nav-tab${tab === t ? ' active' : ''}`} onClick={() => onTab(t)}>
            {t}
          </button>
        ))}
      </div>
      {isLive ? (
        <div className="live-pill">
          <span className="live-dot" />
          LIVE
        </div>
      ) : (
        <div style={{ width: 80 }} />
      )}
    </nav>
  );
}

// ═══════════════════════════════════════════════════════════
// SETUP SCREEN
// ═══════════════════════════════════════════════════════════

function SetupScreen({ onStart, onReplay }) {
  const [nameA, setNameA] = useState('Federer');
  const [nameB, setNameB] = useState('Nadal');
  const [p0A, setP0A] = useState(0.64);
  const [p0B, setP0B] = useState(0.64);

  return (
    <div className="season us">
      <Nav tab="Live" onTab={() => {}} isLive={false} />
      <div className="setup-wrap">
        <div className="setup-card">
          <div className="setup-title">Tennis Engine</div>
          <div className="setup-sub">Configure players to start simulating a match</div>

          <label className="setup-label">Player A name</label>
          <input
            className="sidebar-input"
            style={{ marginBottom: 16 }}
            value={nameA}
            onChange={e => setNameA(e.target.value)}
          />

          <label className="setup-label">Player B name</label>
          <input
            className="sidebar-input"
            style={{ marginBottom: 16 }}
            value={nameB}
            onChange={e => setNameB(e.target.value)}
          />

          <label className="setup-label">A baseline serve win prob: {p0A.toFixed(2)}</label>
          <input
            type="range" min={0.35} max={0.85} step={0.01} value={p0A}
            onChange={e => setP0A(+e.target.value)}
            className="setup-range"
          />

          <label className="setup-label">B baseline serve win prob: {p0B.toFixed(2)}</label>
          <input
            type="range" min={0.35} max={0.85} step={0.01} value={p0B}
            onChange={e => setP0B(+e.target.value)}
            className="setup-range"
            style={{ marginBottom: 24 }}
          />

          <button className="setup-btn primary" onClick={() => onStart(nameA, nameB, p0A, p0B)}>
            Start Match
          </button>
          <button className="setup-btn secondary" onClick={onReplay}>
            Watch Real Match Replay
          </button>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// MATCH SELECT SCREEN
// ═══════════════════════════════════════════════════════════

function MatchSelectScreen({ matches, onSelect, onBack }) {
  return (
    <div className="season us">
      <Nav tab="History" onTab={() => {}} isLive={false} />
      <div className="match-select-page">
        <h1>Select a Match</h1>
        <p>Real ATP/WTA matches with point-by-point data</p>
        <div className="match-grid">
          {matches.map((m, i) => (
            <button key={i} className="match-card" onClick={() => onSelect(m)}>
              <div className="match-card-title">
                <span style={{ color: 'var(--ink)' }}>{m.playerA}</span>
                {' vs '}
                <span style={{ color: 'var(--court-bright)' }}>{m.playerB}</span>
              </div>
              <div className="match-card-meta">{m.year} · {m.surface} · {m.tournament}</div>
              {m.finalScore && <div className="match-card-score">{m.finalScore}</div>}
              <div className="match-card-pts">{m.points.length} points</div>
            </button>
          ))}
        </div>
        <button className="back-link" style={{ marginTop: 28 }} onClick={onBack}>
          ← Back to Setup
        </button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// PROBABILITY CHART
// ═══════════════════════════════════════════════════════════

function ProbChart({ modelHistory, currentIdx, totalPoints, onScrub, isReplay }) {
  const svgRef = useRef(null);
  const [tooltip, setTooltip] = useState(null);

  const W = 600, H = 200, PAD = 24;

  const n = modelHistory?.length ?? 0;
  const pts = n < 1 ? [] : modelHistory.map((v, i) => {
    const x = PAD + (i / Math.max(n - 1, 1)) * (W - 2 * PAD);
    const y = H - PAD - (v * (H - 2 * PAD));
    return [x, y];
  });

  const polyline = pts.map(p => p.join(',')).join(' ');

  function handleMouseMove(e) {
    if (!svgRef.current || pts.length < 2) return;
    const rect = svgRef.current.getBoundingClientRect();
    const svgX = Math.max(0, Math.min(W, ((e.clientX - rect.left) / rect.width) * W));
    const frac = Math.max(0, Math.min(1, (svgX - PAD) / (W - 2 * PAD)));
    const rawIdx = frac * (pts.length - 1);
    const lo = Math.floor(rawIdx), hi = Math.min(pts.length - 1, lo + 1);
    const t = rawIdx - lo;
    const prob = modelHistory[lo] + (modelHistory[hi] - modelHistory[lo]) * t;
    const dotX = pts[lo][0] + (pts[hi][0] - pts[lo][0]) * t;
    const dotY = pts[lo][1] + (pts[hi][1] - pts[lo][1]) * t;
    const ptNum = Math.round(frac * (modelHistory.length - 1)) + 1;
    const cardRect = svgRef.current.closest('.card-s')?.getBoundingClientRect() || rect;
    let tipX = e.clientX - cardRect.left + 14;
    if (tipX + 140 > cardRect.width) tipX = e.clientX - cardRect.left - 150;
    const tipY = Math.max(0, e.clientY - cardRect.top - 20);
    setTooltip({ prob: (prob * 100).toFixed(1), ptNum, dotX, dotY, tipX, tipY });
  }

  return (
    <div style={{ position: 'relative' }}>
      <svg
        ref={svgRef}
        className="chart"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        onMouseMove={pts.length > 1 ? handleMouseMove : undefined}
        onMouseLeave={() => setTooltip(null)}
      >
        <defs>
          <linearGradient id="us-grad" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#ff3d88" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#ff3d88" stopOpacity="0" />
          </linearGradient>
          <filter id="glow"><feGaussianBlur stdDeviation="2.5" /></filter>
        </defs>

        {/* Grid lines */}
        <g stroke="#1e3359" strokeDasharray="2 4" strokeWidth="1">
          <line x1={0} y1={PAD + (H - 2 * PAD) * 0.25} x2={W} y2={PAD + (H - 2 * PAD) * 0.25} />
          <line x1={0} y1={H / 2} x2={W} y2={H / 2} />
          <line x1={0} y1={PAD + (H - 2 * PAD) * 0.75} x2={W} y2={PAD + (H - 2 * PAD) * 0.75} />
        </g>

        {/* Grid labels */}
        <g fontFamily="'IBM Plex Mono', monospace" fontSize="9" fill="#6b7a9b" fontWeight="500">
          <text x={4} y={PAD + (H - 2 * PAD) * 0.25 + 3}>75%</text>
          <text x={4} y={H / 2 + 3}>50%</text>
          <text x={4} y={PAD + (H - 2 * PAD) * 0.75 + 3}>25%</text>
        </g>

        {pts.length > 1 ? (
          <>
            {/* Area fill */}
            <path
              d={`M${pts[0][0]},${pts[0][1]} ${pts.slice(1).map(p => `L${p[0]},${p[1]}`).join(' ')} L${pts[pts.length-1][0]},${H} L${pts[0][0]},${H} Z`}
              fill="url(#us-grad)"
            />
            {/* Glow line */}
            <polyline fill="none" stroke="#ff3d88" strokeWidth="3" points={polyline} filter="url(#glow)" opacity="0.4" />
            {/* Main line */}
            <polyline fill="none" stroke="#ff3d88" strokeWidth="2.2" points={polyline} />
            {/* Live dot */}
            <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r={6} fill="#d7ff3d" stroke="#0a1628" strokeWidth="2" />
            <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r={11} fill="none" stroke="#d7ff3d" strokeOpacity="0.3" strokeWidth="1.5" />
          </>
        ) : (
          <text x={W/2} y={H/2+4} textAnchor="middle" fill="#6b7a9b" fontSize="11"
            fontFamily="'IBM Plex Mono', monospace">
            Log points to see probability trend
          </text>
        )}

        {/* Hover indicator */}
        {tooltip && (
          <>
            <line x1={tooltip.dotX} y1={0} x2={tooltip.dotX} y2={H} stroke="#6b7a9b" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx={tooltip.dotX} cy={tooltip.dotY} r={4} fill="#ff3d88" stroke="#f4f7ff" strokeWidth="1.5" />
          </>
        )}
      </svg>

      {/* Tooltip */}
      {tooltip && (
        <div style={{
          position: 'absolute', top: tooltip.tipY, left: tooltip.tipX,
          background: 'var(--card-2)', border: '1px solid var(--rule-2)',
          borderRadius: 10, padding: '10px 14px', pointerEvents: 'none',
          fontFamily: "'IBM Plex Mono', monospace", fontSize: '0.7rem',
          boxShadow: '0 4px 20px rgba(0,0,0,0.5)', zIndex: 20, minWidth: 120,
        }}>
          <div style={{ color: 'var(--ink-3)', fontWeight: 700, letterSpacing: '0.1em', marginBottom: 6, textTransform: 'uppercase', fontSize: '0.62rem' }}>
            Pt {tooltip.ptNum}
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 14 }}>
            <span style={{ color: 'var(--ink-3)' }}>Model</span>
            <span style={{ color: 'var(--ink)', fontWeight: 700 }}>{tooltip.prob}%</span>
          </div>
        </div>
      )}

      {/* Scrub bar (replay only) */}
      {isReplay && totalPoints > 0 && (
        <div className="scrub">
          <span className="scrub-label">PT {Math.max(0, currentIdx + 1)} / {totalPoints}</span>
          <input
            type="range" min={-1} max={totalPoints - 1} value={currentIdx}
            onChange={e => onScrub(+e.target.value)}
            className="scrub-range"
          />
          <button className="scrub-btn" onClick={() => onScrub(totalPoints - 1)}>
            ● LIVE
          </button>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// SIGNALS PANEL
// ═══════════════════════════════════════════════════════════

function SignalsPanel({ state }) {
  const sig = state?.signals;
  if (!sig) return <div style={{ color: 'var(--ink-3)', fontSize: '0.8rem' }}>No signal data</div>;

  return (
    <>
      {['NMI', 'SMS', 'RMS', 'PMS', 'GPS'].map(name => {
        const a = sig.A?.[name] ?? 0;
        const b = sig.B?.[name] ?? 0;
        return (
          <div key={name} className="sig-row" title={SIGNAL_TOOLTIPS[name]}>
            <span className="sig-val left" style={{ color: 'var(--ink)' }}>{a.toFixed(0)}</span>
            <div className="sig-bar">
              <div className="sig-bar-a" style={{ width: `${Math.min(100, a)}%` }} />
            </div>
            <span className="sig-lbl">{name}</span>
            <div className="sig-bar">
              <div className="sig-bar-b" style={{ width: `${Math.min(100, b)}%` }} />
            </div>
            <span className="sig-val right" style={{ color: 'var(--court-bright)' }}>{b.toFixed(0)}</span>
          </div>
        );
      })}
    </>
  );
}

// ═══════════════════════════════════════════════════════════
// POINT BY POINT
// ═══════════════════════════════════════════════════════════

function PointByPoint({ history, nameA, nameB }) {
  if (!history || history.length === 0) {
    return (
      <div style={{ color: 'var(--ink-3)', fontSize: '0.8rem', padding: '16px 0' }}>
        No points logged yet
      </div>
    );
  }

  return (
    <div className="recap">
      <div className="set-hdr">
        <span>Recent Points</span>
        <span>{history.length} total</span>
      </div>
      {history.slice(0, 40).map((h, i) => (
        <div key={i} className="game-row" style={{ padding: '7px 0' }}>
          <span className="game-score" style={{ fontSize: '0.76rem', minWidth: 32, color: 'var(--ink-3)', fontFamily: "'IBM Plex Mono', monospace" }}>
            {h.pointNumber ?? (history.length - i)}
          </span>
          <span className="game-winner">
            <span style={{ color: h.server === 'A' ? 'var(--ink)' : 'var(--court-bright)', fontWeight: 700 }}>
              {h.server === 'A' ? nameA : nameB}
            </span>
            {' serves · '}
            <span style={{ color: h.winner === 'A' ? 'var(--ink)' : 'var(--court-bright)', fontWeight: 700 }}>
              {h.winner === 'A' ? nameA : nameB}
            </span>
            {' wins'}
          </span>
          <div className="game-dots">
            <div className={`pt-dot pt-${h.winner === 'A' ? 'a' : 'b'}`} />
          </div>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// MATCH STATS
// ═══════════════════════════════════════════════════════════

function MatchStats({ history, state }) {
  const stats = useMemo(() => {
    if (!history || history.length === 0) return null;
    const totalA = history.filter(h => h.winner === 'A').length;
    const totalB = history.filter(h => h.winner === 'B').length;
    const serveA = history.filter(h => h.server === 'A');
    const serveB = history.filter(h => h.server === 'B');
    const wServeA = serveA.filter(h => h.winner === 'A').length;
    const wServeB = serveB.filter(h => h.winner === 'B').length;
    const wRetA = serveB.filter(h => h.winner === 'A').length;
    const wRetB = serveA.filter(h => h.winner === 'B').length;
    const pct = (n, d) => d > 0 ? `${n}/${d} · ${Math.round(n / d * 100)}%` : '—';
    return {
      totalA, totalB,
      serveA: pct(wServeA, serveA.length), serveB: pct(wServeB, serveB.length),
      retA: pct(wRetA, serveB.length), retB: pct(wRetB, serveA.length),
    };
  }, [history]);

  const s = state?.score;

  const rows = [
    { label: 'Points won',    a: stats?.totalA ?? 0,     b: stats?.totalB ?? 0 },
    { label: 'Won on serve',  a: stats?.serveA ?? '—',   b: stats?.serveB ?? '—' },
    { label: 'Won on return', a: stats?.retA ?? '—',     b: stats?.retB ?? '—' },
    { label: 'Sets',          a: s?.setsA ?? 0,          b: s?.setsB ?? 0 },
    { label: 'Games (curr)',  a: s?.gamesA ?? 0,         b: s?.gamesB ?? 0 },
  ];

  return (
    <>
      {rows.map(r => (
        <div key={r.label} className="stat-row">
          <span className="stat-label">{r.label}</span>
          <span className="stat-val a">{r.a}</span>
          <span className="stat-val b">{r.b}</span>
        </div>
      ))}
    </>
  );
}

// ═══════════════════════════════════════════════════════════
// HERO CARD
// ═══════════════════════════════════════════════════════════

function scLabel(me, opp) {
  const L = ['0', '15', '30', '40'];
  if (me >= 3 && opp >= 3) {
    if (me === opp) return 'D';
    return me > opp ? 'AD' : '40';
  }
  return L[Math.min(me, 3)] ?? '0';
}

function HeroCard({ state, nameA, nameB, matchData }) {
  const s = state?.score;
  const p = state?.probabilities;

  const modelPct = p?.matchA != null ? (p.matchA * 100).toFixed(1) + '%' : '—';
  const bookPct  = p?.bookmakerA != null ? (p.bookmakerA * 100).toFixed(1) + '%' : '—';
  const edge = (p?.matchA != null && p?.bookmakerA != null)
    ? ((p.matchA - p.bookmakerA) >= 0 ? '+' : '') + (p.matchA - p.bookmakerA).toFixed(3)
    : '—';

  const sA = s?.setsA ?? 0, sB = s?.setsB ?? 0;
  const gA = s?.gamesA ?? 0, gB = s?.gamesB ?? 0;
  const pA = s?.pointsA ?? 0, pB = s?.pointsB ?? 0;
  const server = s?.server ?? 'A';
  const isTB = s?.isTiebreak ?? false;

  return (
    <div className="hero">
      <div className="hero-head">
        <div>
          <div className="tournament">
            {matchData?.tournament ?? 'Tennis Engine'} · {matchData?.surface ?? 'Hard'}
          </div>
          <div className="matchup">
            {nameA}
            <span className="v" style={{ color: 'var(--volt)' }}>vs</span>
            <em style={{ fontStyle: 'normal', color: 'var(--court-bright)' }}>{nameB}</em>
          </div>
          {matchData?.year && (
            <div style={{ color: 'var(--ink-3)', fontSize: '0.7rem', marginTop: 8, letterSpacing: '0.12em', textTransform: 'uppercase', fontWeight: 500 }}>
              {matchData.year}
            </div>
          )}
        </div>
        <div className="date-badge">HARD · BO3</div>
      </div>

      <div className="score-grid">
        <table className="score-tbl">
          <thead>
            <tr>
              <th className="name" />
              <th>S1</th><th>S2</th><th>S3</th><th>PTS</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="name-cell">
                {server === 'A' ? <span className="serve-mk" /> : <span className="serve-spacer" />}
                {nameA}
              </td>
              <td className={`set${sA > sB ? ' won' : ''}`}>{sA > 0 || sB > 0 ? sA : '—'}</td>
              <td className="set">—</td>
              <td className="set live">{gA}</td>
              <td className="pts">{isTB ? pA : scLabel(pA, pB)}</td>
            </tr>
            <tr>
              <td className="name-cell">
                {server === 'B' ? <span className="serve-mk" /> : <span className="serve-spacer" />}
                {nameB}
              </td>
              <td className={`set${sB > sA ? ' won' : ''}`}>{sA > 0 || sB > 0 ? sB : '—'}</td>
              <td className="set">—</td>
              <td className="set live">{gB}</td>
              <td className="pts">{isTB ? pB : scLabel(pB, pA)}</td>
            </tr>
          </tbody>
        </table>

        <div className="prob-block">
          <div className="prob">
            <div className="prob-label">Model</div>
            <div className="prob-val a">{modelPct}</div>
            <div className="prob-sub">P({nameA.split(/\s+/).pop()})</div>
          </div>
          <div className="prob">
            <div className="prob-label">Book</div>
            <div className="prob-val b">{bookPct}</div>
            <div className="prob-sub">Pinnacle</div>
          </div>
          <div className="prob">
            <div className="prob-label">Edge ⚡</div>
            <div className="prob-val edge">{edge}</div>
            <div className="prob-sub">{edge !== '—' && parseFloat(edge) > 0.02 ? 'Alert' : 'Neutral'}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// MOMENTUM BAR
// ═══════════════════════════════════════════════════════════

function MomentumBar({ state, nameA, nameB }) {
  const d = state?.dominance;
  const dA = d?.D_A ?? 50, dB = d?.D_B ?? 50;
  const total = dA + dB || 1;
  const pctA = (dA / total) * 100;

  return (
    <div className="momentum">
      <div className="mom-head">
        <span className="section-lbl">Momentum</span>
        <span className="mom-meta">
          D: {dA.toFixed(1)} — {dB.toFixed(1)} · Δ{dA - dB >= 0 ? '+' : ''}{(dA - dB).toFixed(1)}
        </span>
      </div>
      <div className="mom-names">
        <span style={{ color: 'var(--magenta)' }}>{nameA.toUpperCase()} · {pctA.toFixed(1)}%</span>
        <span style={{ color: 'var(--court-bright)' }}>{(100 - pctA).toFixed(1)}% · {nameB.toUpperCase()}</span>
      </div>
      <div className="mom-track">
        <div className="mom-fill" style={{ width: `${pctA}%` }} />
        <div className="mom-center" />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// MATCH DETAIL (shared centre layout)
// ═══════════════════════════════════════════════════════════

function MatchDetail({ state, nameA, nameB, modelHistory, currentIdx, totalPoints, onScrub, isReplay, matchData }) {
  const [probTab, setProbTab] = useState(0);

  const displayHistory = probTab === 0 ? modelHistory : modelHistory.map(v => 1 - v);
  const displayName = probTab === 0 ? nameA : nameB;

  return (
    <div className="page" style={{ flex: 1, minWidth: 0 }}>
      <HeroCard state={state} nameA={nameA} nameB={nameB} matchData={matchData} />
      <MomentumBar state={state} nameA={nameA} nameB={nameB} />

      <div className="row">
        <div className="card-s">
          <div className="card-hdr">
            <span className="section-lbl">Win Probability</span>
            <div className="toggle">
              <button className={probTab === 0 ? 'active' : ''} onClick={() => setProbTab(0)}>{nameA}</button>
              <button className={probTab === 1 ? 'active' : ''} onClick={() => setProbTab(1)}>{nameB}</button>
            </div>
          </div>
          <ProbChart
            modelHistory={displayHistory}
            currentIdx={currentIdx}
            totalPoints={totalPoints}
            onScrub={onScrub}
            isReplay={isReplay}
            nameA={displayName}
          />
        </div>

        <div className="card-s">
          <div className="card-hdr">
            <span className="section-lbl">Signals</span>
          </div>
          <SignalsPanel state={state} />
        </div>
      </div>

      <div className="row-2">
        <div className="card-s">
          <div className="card-hdr">
            <span className="section-lbl">Point by Point</span>
          </div>
          <PointByPoint history={state?.history ?? []} nameA={nameA} nameB={nameB} />
        </div>

        <div className="card-s">
          <div className="card-hdr">
            <span className="section-lbl">Match Stats</span>
          </div>
          <MatchStats history={state?.history ?? []} state={state} />
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// MATCH VIEW (manual entry)
// ═══════════════════════════════════════════════════════════

function MatchView({ engine, nameA, nameB, onNewMatch }) {
  const [state, setState] = useState(() => engine.getState());
  const [rally, setRally] = useState(3);
  const [isFirst, setIsFirst] = useState(true);
  const [speed, setSpeed] = useState('');
  const matchHistory = useRef([]);

  const logPoint = (winner) => {
    engine.logPoint({
      server: state.score.server,
      winner,
      rallyLength: rally,
      serveSpeed: speed ? +speed : null,
      isFirstServe: isFirst,
    });
    const newState = engine.getState();
    if (newState.history.length > 0) {
      newState.history[0].server = state.score.server;
      newState.history[0].winner = winner;
      newState.history[0].rallyLength = rally;
    }
    matchHistory.current = [...matchHistory.current, newState.probabilities.matchA];
    setState(newState);
    setSpeed('');
  };

  const s = state.score;

  return (
    <div className="season us" style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <Nav tab="Live" onTab={() => {}} isLive />
      <div style={{ display: 'flex', flex: 1 }}>

        {/* ── Point Logger Sidebar ── */}
        <div className="sidebar">
          <div>
            <div style={{ fontWeight: 700, fontSize: '0.82rem', color: 'var(--ink)', marginBottom: 4 }}>
              Log Point
            </div>
            <div style={{ fontSize: '0.68rem', color: 'var(--ink-3)' }}>
              Serving:{' '}
              <span style={{ color: s.server === 'A' ? 'var(--ink)' : 'var(--court-bright)', fontWeight: 700 }}>
                {s.server === 'A' ? nameA : nameB}
              </span>
              {s.isTiebreak && <span style={{ color: 'var(--volt)', marginLeft: 6, fontWeight: 700 }}>TB</span>}
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Rally length</div>
            <div className="pill-row">
              {RALLY_OPTIONS.map(r => {
                const val = r === '15+' ? 15 : r;
                return (
                  <button key={r} className={`pill${rally === val ? ' active' : ''}`}
                    onClick={() => setRally(val)}>
                    {r}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Serve</div>
            <div className="tog-row">
              <button className={`tog${isFirst ? ' a-active' : ''}`} onClick={() => setIsFirst(true)}>1st</button>
              <button className={`tog${!isFirst ? ' a-active' : ''}`} onClick={() => setIsFirst(false)}>2nd</button>
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Serve speed (km/h)</div>
            <input className="sidebar-input" type="number" placeholder="optional"
              value={speed} onChange={e => setSpeed(e.target.value)} />
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Who won?</div>
            <button className="win-btn a" style={{ marginBottom: 8 }} onClick={() => logPoint('A')}>
              {nameA} Won
            </button>
            <button className="win-btn b" onClick={() => logPoint('B')}>
              {nameB} Won
            </button>
          </div>

          <div style={{ marginTop: 'auto' }}>
            <button className="back-link" onClick={onNewMatch}>← New Match</button>
          </div>
        </div>

        <MatchDetail
          state={state}
          nameA={nameA}
          nameB={nameB}
          modelHistory={matchHistory.current}
          currentIdx={matchHistory.current.length - 1}
          totalPoints={matchHistory.current.length}
          onScrub={() => {}}
          isReplay={false}
          matchData={null}
        />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// REPLAY VIEW
// ═══════════════════════════════════════════════════════════

function ReplayView({ matchData, onBack, onManual }) {
  const { playerA, playerB, year, points, p0_A, p0_B } = matchData;
  const totalPoints = points.length;

  const engineRef      = useRef(null);
  const timerRef       = useRef(null);
  const playingRef     = useRef(false);
  const currentIdxRef  = useRef(-1);
  const speedMsRef     = useRef(1000);
  const matchHistoryRef = useRef([]);

  const [currentIdx, setCurrentIdx] = useState(-1);
  const [playing, setPlaying]       = useState(false);
  const [speedMs, setSpeedMs]       = useState(1000);
  const [state, setState]           = useState(null);

  useEffect(() => { speedMsRef.current = speedMs; }, [speedMs]);
  useEffect(() => { playingRef.current = playing; }, [playing]);
  useEffect(() => { currentIdxRef.current = currentIdx; }, [currentIdx]);

  const processPoint = useCallback((engine, pt) => {
    engine.logPoint({
      server: pt.server, winner: pt.winner,
      rallyLength: pt.rallyLength, serveSpeed: pt.serveSpeed, isFirstServe: pt.isFirstServe,
    });
    const s = engine.getState();
    if (s.history.length > 0) {
      s.history[0].server = pt.server;
      s.history[0].winner = pt.winner;
      s.history[0].rallyLength = pt.rallyLength;
    }
    return s;
  }, []);

  const buildToIndex = useCallback((targetIdx) => {
    const eng = new TennisEngine(playerA, playerB, p0_A, p0_B);
    const history = [];
    for (let i = 0; i <= targetIdx && i < totalPoints; i++) {
      const s = processPoint(eng, points[i]);
      history.push(s.probabilities.matchA);
    }
    engineRef.current = eng;
    matchHistoryRef.current = history;
    return targetIdx >= 0 ? eng.getState() : null;
  }, [playerA, playerB, p0_A, p0_B, points, totalPoints, processPoint]);

  useEffect(() => {
    const eng = new TennisEngine(playerA, playerB, p0_A, p0_B);
    engineRef.current = eng;
    setState(eng.getState());
    matchHistoryRef.current = [];
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [playerA, playerB, p0_A, p0_B]);

  const stopTimer = useCallback(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
  }, []);

  const scheduleNextTick = useCallback(() => {
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (!playingRef.current) return;
      const nextIdx = currentIdxRef.current + 1;
      if (nextIdx >= totalPoints) { setPlaying(false); playingRef.current = false; return; }
      const s = processPoint(engineRef.current, points[nextIdx]);
      matchHistoryRef.current = [...matchHistoryRef.current, s.probabilities.matchA];
      currentIdxRef.current = nextIdx;
      setCurrentIdx(nextIdx);
      setState(s);
      scheduleNextTick();
    }, speedMsRef.current);
  }, [totalPoints, points, processPoint]);

  const togglePlay = useCallback(() => {
    if (playing) { stopTimer(); setPlaying(false); playingRef.current = false; return; }
    if (currentIdxRef.current >= totalPoints - 1) {
      const eng = new TennisEngine(playerA, playerB, p0_A, p0_B);
      engineRef.current = eng; matchHistoryRef.current = [];
      currentIdxRef.current = -1; setCurrentIdx(-1); setState(eng.getState());
    }
    setPlaying(true); playingRef.current = true; scheduleNextTick();
  }, [playing, totalPoints, playerA, playerB, p0_A, p0_B, stopTimer, scheduleNextTick]);

  const changeSpeed = useCallback((ms) => {
    setSpeedMs(ms); speedMsRef.current = ms;
    if (playingRef.current && timerRef.current) {
      clearTimeout(timerRef.current); timerRef.current = null; scheduleNextTick();
    }
  }, [scheduleNextTick]);

  const scrubTo = useCallback((targetIdx) => {
    stopTimer(); setPlaying(false); playingRef.current = false;
    const newState = buildToIndex(targetIdx);
    currentIdxRef.current = targetIdx;
    setState(newState || engineRef.current.getState());
    setCurrentIdx(targetIdx);
  }, [buildToIndex, stopTimer]);

  const stepNext = useCallback(() => {
    if (currentIdxRef.current >= totalPoints - 1) return;
    const nextIdx = currentIdxRef.current + 1;
    const s = processPoint(engineRef.current, points[nextIdx]);
    matchHistoryRef.current.push(s.probabilities.matchA);
    currentIdxRef.current = nextIdx; setCurrentIdx(nextIdx); setState(s);
  }, [totalPoints, points, processPoint]);

  const stepPrev = () => { if (currentIdx > -1) scrubTo(currentIdx - 1); };

  if (!state) return null;

  const curPt = currentIdx >= 0 && currentIdx < totalPoints ? points[currentIdx] : null;

  return (
    <div className="season us" style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <Nav tab="History" onTab={() => {}} isLive={playing} />
      <div style={{ display: 'flex', flex: 1 }}>

        {/* ── Replay Controls Sidebar ── */}
        <div className="sidebar">
          <div>
            <div style={{ fontWeight: 700, fontSize: '0.88rem', color: 'var(--ink)', lineHeight: 1.3, marginBottom: 4 }}>
              <span>{playerA}</span>
              <span style={{ color: 'var(--ink-3)', fontWeight: 400 }}> vs </span>
              <span style={{ color: 'var(--court-bright)' }}>{playerB}</span>
            </div>
            <div style={{ fontSize: '0.7rem', color: 'var(--ink-3)' }}>
              {matchData.tournament} · {year}
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Playback</div>
            <div className="ctrl-row">
              <button className="ctrl-btn" onClick={() => scrubTo(-1)} title="Restart">⏮</button>
              <button className="ctrl-btn" onClick={() => scrubTo(Math.max(-1, currentIdx - 10))} title="-10">⏪</button>
              <button className="ctrl-btn" onClick={stepPrev} title="Previous">◀</button>
              <button
                className={`play-btn ${playing ? 'playing' : 'paused'}`}
                onClick={togglePlay}
              >
                {playing ? '⏸' : '▶'}
              </button>
              <button className="ctrl-btn" onClick={stepNext} title="Next">▶</button>
              <button className="ctrl-btn" onClick={() => scrubTo(Math.min(totalPoints - 1, currentIdx + 10))} title="+10">⏩</button>
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Speed</div>
            <div className="pill-row">
              {SPEED_OPTIONS.map(opt => (
                <button key={opt.label} className={`pill${speedMs === opt.ms ? ' active' : ''}`}
                  onClick={() => changeSpeed(opt.ms)}>
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          <div className="sidebar-section">
            <div className="sidebar-label">Point {Math.max(0, currentIdx + 1)} / {totalPoints}</div>
            <input
              type="range" min={-1} max={totalPoints - 1} value={currentIdx}
              onChange={e => scrubTo(+e.target.value)}
              className="sidebar-range"
            />
          </div>

          {curPt && (
            <div className="sidebar-mono">
              <div>Srv: <span style={{ color: curPt.server === 'A' ? 'var(--ink)' : 'var(--court-bright)', fontWeight: 700 }}>
                {curPt.server === 'A' ? playerA : playerB}
              </span></div>
              <div>Won: <span style={{ color: curPt.winner === 'A' ? 'var(--ink)' : 'var(--court-bright)', fontWeight: 700 }}>
                {curPt.winner === 'A' ? playerA : playerB}
              </span></div>
              <div>Rally: {curPt.rallyLength ?? '—'}</div>
              <div>Serve: {curPt.isFirstServe ? '1st' : '2nd'}</div>
            </div>
          )}

          <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
            <button className="back-link" onClick={onBack}>← Match List</button>
            <button className="back-link" onClick={() => onManual(playerA, playerB, p0_A, p0_B)}>
              Switch to Manual
            </button>
          </div>
        </div>

        <MatchDetail
          state={state}
          nameA={playerA}
          nameB={playerB}
          modelHistory={matchHistoryRef.current}
          currentIdx={currentIdx}
          totalPoints={totalPoints}
          onScrub={scrubTo}
          isReplay
          matchData={matchData}
        />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════
// ROOT APP
// ═══════════════════════════════════════════════════════════

export default function App() {
  const engineRef     = useRef(null);
  const [screen, setScreen]           = useState('setup');
  const [names, setNames]             = useState({ a: '', b: '' });
  const [matchKey, setMatchKey]       = useState(0);
  const [replayMatches, setReplayMatches] = useState(null);
  const [selectedReplay, setSelectedReplay] = useState(null);
  const [loadError, setLoadError]     = useState(null);

  const handleStart = (nameA, nameB, p0A, p0B) => {
    engineRef.current = new TennisEngine(nameA, nameB, p0A, p0B);
    setNames({ a: nameA, b: nameB });
    setMatchKey(k => k + 1);
    setScreen('match');
  };

  const handleReplay = async () => {
    try {
      setLoadError(null);
      const res = await fetch('/replay_matches.json');
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      const data = await res.json();
      if (!Array.isArray(data) || data.length === 0) throw new Error('No matches found in file');
      setReplayMatches(data);
      setScreen('matchSelect');
    } catch (err) {
      setLoadError(`Failed to load replay data: ${err.message}. Run the export script first.`);
    }
  };

  if (screen === 'setup') {
    return (
      <>
        <SetupScreen onStart={handleStart} onReplay={handleReplay} />
        {loadError && (
          <div style={{
            position: 'fixed', bottom: 20, left: '50%', transform: 'translateX(-50%)',
            background: '#ff3d88', color: '#fff', padding: '10px 20px', borderRadius: 8,
            fontSize: '0.8rem', maxWidth: 500, textAlign: 'center',
            fontFamily: "'Space Grotesk', sans-serif",
          }}>
            {loadError}
          </div>
        )}
      </>
    );
  }

  if (screen === 'matchSelect' && replayMatches) {
    return (
      <MatchSelectScreen
        matches={replayMatches}
        onSelect={m => { setSelectedReplay(m); setScreen('replay'); }}
        onBack={() => setScreen('setup')}
      />
    );
  }

  if (screen === 'replay' && selectedReplay) {
    return (
      <ReplayView
        key={selectedReplay.matchId}
        matchData={selectedReplay}
        onBack={() => setScreen('matchSelect')}
        onManual={handleStart}
      />
    );
  }

  return (
    <MatchView
      key={matchKey}
      engine={engineRef.current}
      nameA={names.a}
      nameB={names.b}
      onNewMatch={() => setScreen('setup')}
    />
  );
}
