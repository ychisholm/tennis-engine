// Tennis Engine — full dominance score engine in JavaScript
// Matches the Python spec for all signals, temporal engine, and Markov chain.

const LN2 = Math.log(2);

function sigmoid(x) {
  return 1 / (1 + Math.exp(-x));
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

// Memoised game-win probability from a given point state
const _gameCache = new Map();
function gameWinProb(p, pA, pB) {
  // pA, pB in {0,1,2,3} representing 0,15,30,40
  if (pA >= 3 && pB >= 3) {
    // deuce situation
    return (p * p) / (p * p + (1 - p) * (1 - p));
  }
  if (pA >= 4) return 1;
  if (pB >= 4) return 0;
  const key = `${p.toFixed(6)}_${pA}_${pB}`;
  if (_gameCache.has(key)) return _gameCache.get(key);
  const win = p * gameWinProb(p, pA + 1, pB) + (1 - p) * gameWinProb(p, pA, pB + 1);
  _gameCache.set(key, win);
  return win;
}

function G(p) {
  // probability server wins a full game from 0-0
  _gameCache.clear();
  return gameWinProb(p, 0, 0);
}

// Game win prob from current point score
function gameWinProbFromScore(p, ptsA, ptsB, isDeuce, advPlayer) {
  _gameCache.clear();
  if (isDeuce) {
    if (advPlayer === 'A') {
      // A needs 1 point to win from advantage
      return p * 1 + (1 - p) * gameWinProb(p, 3, 3);
    } else if (advPlayer === 'B') {
      return p * gameWinProb(p, 3, 3) + (1 - p) * 0;
    }
    return gameWinProb(p, 3, 3);
  }
  return gameWinProb(p, ptsA, ptsB);
}

// Set win prob from current game score via enumeration
function setWinProb(g, h, gA, gB, tiebreakP) {
  // g = P(server A holds), h = P(A breaks B's serve)
  // Enumerate from (gA, gB) to set completion
  const memo = new Map();
  function rec(a, b) {
    if (a >= 6 && b <= a - 2) return 1;
    if (b >= 6 && a <= b - 2) return 0;
    if (a === 7) return 1;
    if (b === 7) return 0;
    if (a === 6 && b === 6) {
      // tiebreak
      return tiebreakP;
    }
    const key = `${a}_${b}`;
    if (memo.has(key)) return memo.get(key);
    // Determine who serves next game: total games played = a + b
    // A serves first game of set. Server alternates each game.
    const totalGames = a + b;
    const aServes = totalGames % 2 === 0;
    let prob;
    if (aServes) {
      // A serves: prob A wins this game = g
      prob = g * rec(a + 1, b) + (1 - g) * rec(a, b + 1);
    } else {
      // B serves: prob A wins this game = h (A breaks)
      prob = h * rec(a + 1, b) + (1 - h) * rec(a, b + 1);
    }
    memo.set(key, prob);
    return prob;
  }
  return rec(gA, gB);
}

// Match win prob (best of 3 sets) from current set score
function matchWinProb(setWinP, sA, sB) {
  const memo = new Map();
  function rec(a, b) {
    if (a === 2) return 1;
    if (b === 2) return 0;
    const key = `${a}_${b}`;
    if (memo.has(key)) return memo.get(key);
    const p = setWinP; // simplified: same set win prob for remaining sets
    const val = p * rec(a + 1, b) + (1 - p) * rec(a, b + 1);
    memo.set(key, val);
    return val;
  }
  return rec(sA, sB);
}


export default class TennisEngine {
  constructor(nameA = 'Player A', nameB = 'Player B', p0A = 0.64, p0B = 0.64) {
    this.nameA = nameA;
    this.nameB = nameB;
    this.p0A = p0A;
    this.p0B = p0B;

    // Temporal params
    this.lambda = 10;
    this.k = 0.04;
    this.sigma = 40;
    this.w_r = 0.60;
    this.w_s = 0.40;
    this.gamma = 0.20;

    this._reset();
  }

  _reset() {
    // Score state
    this.pointsA = 0; // 0,1,2,3 for 0,15,30,40
    this.pointsB = 0;
    this.gamesA = 0;
    this.gamesB = 0;
    this.setsA = 0;
    this.setsB = 0;
    this.isDeuce = false;
    this.isAdvantage = null; // null, "A", "B"
    this.currentServer = 'A';
    this.isTiebreak = false;
    this.tiebreakPointCount = 0;

    // Game tracking
    this.gameHistory = [];
    this.currentGamePoints = [];
    this.setIndex = 0;
    this.globalGameIndex = 0;
    this.setStartGameIndex = 0;

    // Point tracking
    this.totalPointsPlayed = 0;

    // Per-game break point tracking
    this.currentGameMaxPressure = 0; // 0, 0.2, 0.5, 0.8, 1.0
    this.currentGameHadBreakPoint = false;
    this.currentGameBreakPointCount = 0;

    // Signal tracking
    // Rolling serve/return point windows per player
    this.servePointsA = []; // last 15
    this.servePointsB = [];
    this.returnPointsA = []; // last 15
    this.returnPointsB = [];

    // Serve speed tracking
    this.allSpeedsA = [];
    this.allSpeedsB = [];
    this.setSpeedsA = {}; // { setIndex: [speeds] }
    this.setSpeedsB = {};

    // Rally buckets per player
    this.rallyBucketsA = { short: { won: 0, played: 0 }, medium: { won: 0, played: 0 }, long: { won: 0, played: 0 } };
    this.rallyBucketsB = { short: { won: 0, played: 0 }, medium: { won: 0, played: 0 }, long: { won: 0, played: 0 } };

    // NMI running max
    this.runningMaxNMI_A = 0;
    this.runningMaxNMI_B = 0;

    // GPS running max
    this.runningMaxGPS_A = 0;
    this.runningMaxGPS_B = 0;

    // Temporal engine
    this.snapshots = [];
    this.setSnapshots = [];
    this.prevSetLayerA = 50;
    this.prevSetLayerB = 50;
    this.setLayerInitA = null;
    this.setLayerInitB = null;

    // History for UI
    this.pointHistory = [];
  }

  logPoint(pointData) {
    const { server, winner, rallyLength, serveSpeed, isFirstServe } = pointData;
    this.totalPointsPlayed++;

    // Track serve/return points
    const serveEntry = { won: winner === server, isFirst: isFirstServe, speed: serveSpeed };
    const returnPlayer = server === 'A' ? 'B' : 'A';
    const returnEntry = { won: winner === returnPlayer };

    if (server === 'A') {
      this.servePointsA.push(serveEntry);
      if (this.servePointsA.length > 15) this.servePointsA.shift();
      this.returnPointsB.push(returnEntry);
      if (this.returnPointsB.length > 15) this.returnPointsB.shift();
    } else {
      this.servePointsB.push(serveEntry);
      if (this.servePointsB.length > 15) this.servePointsB.shift();
      this.returnPointsA.push(returnEntry);
      if (this.returnPointsA.length > 15) this.returnPointsA.shift();
    }

    // Track serve speed
    if (serveSpeed != null && serveSpeed > 0) {
      if (server === 'A') {
        this.allSpeedsA.push(serveSpeed);
        if (!this.setSpeedsA[this.setIndex]) this.setSpeedsA[this.setIndex] = [];
        this.setSpeedsA[this.setIndex].push(serveSpeed);
      } else {
        this.allSpeedsB.push(serveSpeed);
        if (!this.setSpeedsB[this.setIndex]) this.setSpeedsB[this.setIndex] = [];
        this.setSpeedsB[this.setIndex].push(serveSpeed);
      }
    }

    // Rally bucket tracking
    const bucket = rallyLength <= 3 ? 'short' : rallyLength <= 8 ? 'medium' : 'long';
    // Both players participated in this rally
    if (winner === 'A') {
      this.rallyBucketsA[bucket].won++;
    } else {
      this.rallyBucketsB[bucket].won++;
    }
    this.rallyBucketsA[bucket].played++;
    this.rallyBucketsB[bucket].played++;

    // Store point in current game
    this.currentGamePoints.push({ server, winner, rallyLength, serveSpeed, isFirstServe });

    // Check break point pressure BEFORE scoring this point
    this._updateGamePressure(server);

    // Score the point
    this._scorePoint(winner);
  }

  _updateGamePressure(server) {
    // Check current score state for break point pressure
    // Break point = returner is one point from winning the game
    const returner = server === 'A' ? 'B' : 'A';
    const serverPts = server === 'A' ? this.pointsA : this.pointsB;
    const returnerPts = server === 'A' ? this.pointsB : this.pointsA;

    if (this.isAdvantage === returner) {
      // Break point (advantage to returner)
      this.currentGameHadBreakPoint = true;
      this.currentGameBreakPointCount = Math.max(this.currentGameBreakPointCount, 1);
      this.currentGameMaxPressure = Math.max(this.currentGameMaxPressure, 0.5);
    } else if (this.isDeuce && !this.isAdvantage) {
      // Deuce without break point
      this.currentGameMaxPressure = Math.max(this.currentGameMaxPressure, 0.2);
    } else if (!this.isTiebreak) {
      // Check for break point situations in normal scoring
      // Returner at 40, server at less than 40
      if (returnerPts >= 3 && serverPts < 3) {
        this.currentGameHadBreakPoint = true;
        // Count how many break points based on score
        if (serverPts === 0) {
          // 0-40: 3 break points
          this.currentGameBreakPointCount = Math.max(this.currentGameBreakPointCount, 3);
          this.currentGameMaxPressure = Math.max(this.currentGameMaxPressure, 1.0);
        } else if (serverPts === 1) {
          // 15-40: 2 break points
          this.currentGameBreakPointCount = Math.max(this.currentGameBreakPointCount, 2);
          this.currentGameMaxPressure = Math.max(this.currentGameMaxPressure, 0.8);
        } else if (serverPts === 2) {
          // 30-40: 1 break point
          this.currentGameBreakPointCount = Math.max(this.currentGameBreakPointCount, 1);
          this.currentGameMaxPressure = Math.max(this.currentGameMaxPressure, 0.5);
        }
      }
    }
  }

  _scorePoint(winner) {
    if (this.isTiebreak) {
      this._scoreTiebreakPoint(winner);
      return;
    }

    // Handle deuce/advantage
    if (this.isDeuce || this.isAdvantage) {
      if (this.isAdvantage) {
        if (winner === this.isAdvantage) {
          // Game won
          this._winGame(winner);
          return;
        } else {
          // Back to deuce
          this.isAdvantage = null;
          this.isDeuce = true;
          this._afterPointScored();
          return;
        }
      }
      // At deuce
      this.isAdvantage = winner;
      this.isDeuce = false;
      this._afterPointScored();
      return;
    }

    // Normal scoring
    if (winner === 'A') {
      this.pointsA++;
    } else {
      this.pointsB++;
    }

    // Check for deuce
    if (this.pointsA === 3 && this.pointsB === 3) {
      this.isDeuce = true;
      this._afterPointScored();
      return;
    }

    // Check for game won
    if (this.pointsA >= 4) {
      this._winGame('A');
      return;
    }
    if (this.pointsB >= 4) {
      this._winGame('B');
      return;
    }

    this._afterPointScored();
  }

  _scoreTiebreakPoint(winner) {
    if (winner === 'A') this.pointsA++;
    else this.pointsB++;
    this.tiebreakPointCount++;

    // Check for tiebreak win: first to 7 with 2 clear
    if (this.pointsA >= 7 || this.pointsB >= 7) {
      if (Math.abs(this.pointsA - this.pointsB) >= 2) {
        this._winGame(this.pointsA > this.pointsB ? 'A' : 'B');
        return;
      }
    }

    // Server changes every 2 points in tiebreak (after first point)
    if (this.tiebreakPointCount === 1 || (this.tiebreakPointCount > 1 && (this.tiebreakPointCount - 1) % 2 === 0)) {
      this.currentServer = this.currentServer === 'A' ? 'B' : 'A';
    }

    this._afterPointScored();
  }

  _winGame(winner) {
    // Record completed game
    const gameObj = {
      server: this.currentServer,
      pointsPlayed: this.currentGamePoints.length,
      serverWon: winner === this.currentServer,
      gameIndex: this.globalGameIndex,
      setIndex: this.setIndex,
      hadBreakPoint: this.currentGameHadBreakPoint,
      maxPressureReached: this.currentGameMaxPressure,
      isTiebreak: this.isTiebreak
    };
    this.gameHistory.push(gameObj);
    this.globalGameIndex++;

    // Update game score
    if (winner === 'A') this.gamesA++;
    else this.gamesB++;

    // Reset game state
    this.pointsA = 0;
    this.pointsB = 0;
    this.isDeuce = false;
    this.isAdvantage = null;
    this.currentGamePoints = [];
    this.currentGameMaxPressure = 0;
    this.currentGameHadBreakPoint = false;
    this.currentGameBreakPointCount = 0;

    // Check for set win
    const wasTiebreak = this.isTiebreak;
    if (this.isTiebreak) {
      this.isTiebreak = false;
      this.tiebreakPointCount = 0;
      this._winSet(winner);
      return;
    }

    if ((this.gamesA >= 6 || this.gamesB >= 6) && Math.abs(this.gamesA - this.gamesB) >= 2) {
      this._winSet(this.gamesA > this.gamesB ? 'A' : 'B');
      return;
    }

    if (this.gamesA === 6 && this.gamesB === 6) {
      this.isTiebreak = true;
      this.tiebreakPointCount = 0;
    }

    // Switch server
    this.currentServer = this.currentServer === 'A' ? 'B' : 'A';

    this._afterPointScored();
  }

  _winSet(winner) {
    if (winner === 'A') this.setsA++;
    else this.setsB++;

    // Temporal: save set layer values before reset
    const setLayerA = this._computeSetLayer('A');
    const setLayerB = this._computeSetLayer('B');
    this.prevSetLayerA = setLayerA;
    this.prevSetLayerB = setLayerB;

    // Reset for new set
    this.gamesA = 0;
    this.gamesB = 0;
    this.setIndex++;
    this.setStartGameIndex = this.globalGameIndex;
    this.setSnapshots = [];
    this.setLayerInitA = this.gamma * setLayerA;
    this.setLayerInitB = this.gamma * setLayerB;

    // Switch server
    this.currentServer = this.currentServer === 'A' ? 'B' : 'A';

    this._afterPointScored();
  }

  _afterPointScored() {
    // Compute signals and store snapshot
    const signals = this._computeAllSignals();
    const compositeA = (signals.A.NMI + signals.A.SMS + signals.A.RMS + signals.A.PMS + signals.A.GPS) / 5;
    const compositeB = (signals.B.NMI + signals.B.SMS + signals.B.RMS + signals.B.PMS + signals.B.GPS) / 5;

    this.snapshots.push({
      pointIndex: this.totalPointsPlayed,
      compositeA,
      compositeB
    });
    this.setSnapshots.push({
      pointIndex: this.totalPointsPlayed,
      compositeA,
      compositeB
    });

    // Compute dominance
    const recA = this._computeRecencyLayer('A');
    const recB = this._computeRecencyLayer('B');
    const setA = this._computeSetLayer('A');
    const setB = this._computeSetLayer('B');

    const D_A = clamp(this.w_r * recA + this.w_s * setA, 0, 100);
    const D_B = clamp(this.w_r * recB + this.w_s * setB, 0, 100);
    const delta = D_A - D_B;

    // p-hat
    const pHatA = clamp(this.p0A + this.k * (2 * sigmoid(delta / this.sigma) - 1), 0.35, 0.85);
    const pHatB = clamp(this.p0B + this.k * (2 * sigmoid(-delta / this.sigma) - 1), 0.35, 0.85);

    // Probabilities
    const gameProb = this._computeGameProb(pHatA, pHatB);
    const setProb = this._computeSetProb(pHatA, pHatB);
    const matchProb = this._computeMatchProb(pHatA, pHatB, setProb);

    // Store history
    this.pointHistory.unshift({
      pointNumber: this.totalPointsPlayed,
      server: this.currentServer,
      winner: this.currentGamePoints.length > 0 ? this.currentGamePoints[this.currentGamePoints.length - 1].winner : (this.gameHistory.length > 0 ? (this.gameHistory[this.gameHistory.length - 1].serverWon ? this.gameHistory[this.gameHistory.length - 1].server : (this.gameHistory[this.gameHistory.length - 1].server === 'A' ? 'B' : 'A')) : 'A'),
      rallyLength: 0,
      D_A,
      D_B,
      matchA: matchProb
    });

    // Correct the history entry with actual point data from the logPoint call
    // This is handled in logPoint's closure via getState
  }

  _computeNMI(player) {
    // NMI for a player: look at games where the OTHER player served (return games for this player)
    const opponent = player === 'A' ? 'B' : 'A';
    let raw = 0;
    const currentGameIdx = this.globalGameIndex;

    for (const game of this.gameHistory) {
      if (game.server !== opponent) continue; // only return games
      if (game.isTiebreak) continue;
      const age = currentGameIdx - game.gameIndex;
      const recency = Math.exp(-LN2 * age / 4); // lambda=4 for NMI
      raw += game.maxPressureReached * recency;
    }

    // Normalise with running max
    if (player === 'A') {
      if (raw > 0 && raw > this.runningMaxNMI_A) this.runningMaxNMI_A = raw;
      return this.runningMaxNMI_A > 0 ? (raw / this.runningMaxNMI_A) * 100 : 0;
    } else {
      if (raw > 0 && raw > this.runningMaxNMI_B) this.runningMaxNMI_B = raw;
      return this.runningMaxNMI_B > 0 ? (raw / this.runningMaxNMI_B) * 100 : 0;
    }
  }

  _computeSMS(player) {
    const servePoints = player === 'A' ? this.servePointsA : this.servePointsB;

    // SMS_1: 1st serve win %
    const firstServes = servePoints.filter(p => p.isFirst);
    const sms1 = firstServes.length >= 3
      ? (firstServes.filter(p => p.won).length / firstServes.length) * 100
      : 50;

    // SMS_2: 2nd serve win %
    const secondServes = servePoints.filter(p => !p.isFirst);
    const sms2 = secondServes.length >= 3
      ? (secondServes.filter(p => p.won).length / secondServes.length) * 100
      : 50;

    // SMS_3: serve speed trend (EMA of last 5 / match avg)
    const allSpeeds = player === 'A' ? this.allSpeedsA : this.allSpeedsB;
    let sms3 = 50;
    if (allSpeeds.length >= 3) {
      const matchAvg = allSpeeds.reduce((a, b) => a + b, 0) / allSpeeds.length;
      const last5 = allSpeeds.slice(-5);
      const alpha = 2 / (5 + 1);
      let ema = last5[0];
      for (let i = 1; i < last5.length; i++) {
        ema = alpha * last5[i] + (1 - alpha) * ema;
      }
      const ratio = clamp(ema / matchAvg, 0.5, 1.5);
      sms3 = clamp(((ratio - 0.5) / 1.0) * 100, 0, 100);
    }

    // SMS_4: hold efficiency this set
    const setGames = this.gameHistory.filter(g => g.gameIndex >= this.setStartGameIndex && g.server === (player === 'A' ? 'A' : 'B') && !g.isTiebreak);
    let sms4 = 50;
    if (setGames.length > 0) {
      const effs = setGames.map(g => g.serverWon ? (4 / g.pointsPlayed) : 0);
      sms4 = (effs.reduce((a, b) => a + b, 0) / effs.length) * 100;
    }

    return 0.35 * sms1 + 0.30 * sms2 + 0.20 * sms3 + 0.15 * sms4;
  }

  _computeRMS(player) {
    const returnPoints = player === 'A' ? this.returnPointsA : this.returnPointsB;

    // RMS_1: return points won %
    const rms1 = returnPoints.length >= 3
      ? (returnPoints.filter(p => p.won).length / returnPoints.length) * 100
      : 50;

    // RMS_2: break conversion rate this set
    const opponent = player === 'A' ? 'B' : 'A';
    const opponentServeGames = this.gameHistory.filter(g => g.gameIndex >= this.setStartGameIndex && g.server === opponent && !g.isTiebreak);
    let breakPointsFaced = 0;
    let breaksConverted = 0;
    for (const g of opponentServeGames) {
      if (g.hadBreakPoint) breakPointsFaced++;
      if (!g.serverWon) breaksConverted++;
    }
    const rms2 = breakPointsFaced > 0 ? (breaksConverted / breakPointsFaced) * 100 : 50;

    // RMS_3: NMI for this player
    const rms3 = this._computeNMI(player);

    return 0.35 * rms1 + 0.25 * rms2 + 0.40 * rms3;
  }

  _computePMS(player) {
    const buckets = player === 'A' ? this.rallyBucketsA : this.rallyBucketsB;

    const pms1 = buckets.short.played >= 3
      ? (buckets.short.won / buckets.short.played) * 100 : 50;
    const pms2 = buckets.medium.played >= 3
      ? (buckets.medium.won / buckets.medium.played) * 100 : 50;
    const pms3 = buckets.long.played >= 3
      ? (buckets.long.won / buckets.long.played) * 100 : 50;

    // PMS_4: serve speed fatigue
    let pms4 = 50;
    const setSpeeds = player === 'A' ? this.setSpeedsA : this.setSpeedsB;
    const set1Speeds = setSpeeds[0];
    const currentSetSpeeds = setSpeeds[this.setIndex];
    if (set1Speeds && set1Speeds.length >= 3 && this.setIndex > 0 && currentSetSpeeds && currentSetSpeeds.length >= 3) {
      const set1Avg = set1Speeds.reduce((a, b) => a + b, 0) / set1Speeds.length;
      const curAvg = currentSetSpeeds.reduce((a, b) => a + b, 0) / currentSetSpeeds.length;
      const fatigueRatio = clamp((set1Avg - curAvg) / set1Avg, 0, 1);
      pms4 = (1 - fatigueRatio) * 100;
    }

    return 0.25 * pms1 + 0.35 * pms2 + 0.30 * pms3 + 0.10 * pms4;
  }

  _computeGPS(player) {
    // GPS computed from the server's perspective: long serve games = bad for server
    // Provided to the returner. So GPS for player X = pressure on opponent's serve games
    const opponent = player === 'A' ? 'B' : 'A';
    const currentGameIdx = this.globalGameIndex;
    let raw = 0;

    for (const game of this.gameHistory) {
      if (game.gameIndex < this.setStartGameIndex) continue;
      if (game.server !== opponent) continue;
      const pressure = Math.max(0, game.pointsPlayed - 4);
      const age = currentGameIdx - game.gameIndex;
      const recency = Math.exp(-LN2 * age / 4);
      raw += pressure * recency;
    }

    if (player === 'A') {
      if (raw > 0 && raw > this.runningMaxGPS_A) this.runningMaxGPS_A = raw;
      return this.runningMaxGPS_A > 0 ? (raw / this.runningMaxGPS_A) * 100 : 0;
    } else {
      if (raw > 0 && raw > this.runningMaxGPS_B) this.runningMaxGPS_B = raw;
      return this.runningMaxGPS_B > 0 ? (raw / this.runningMaxGPS_B) * 100 : 0;
    }
  }

  _computeAllSignals() {
    return {
      A: {
        NMI: this._computeNMI('A'),
        SMS: this._computeSMS('A'),
        RMS: this._computeRMS('A'),
        PMS: this._computePMS('A'),
        GPS: this._computeGPS('A')
      },
      B: {
        NMI: this._computeNMI('B'),
        SMS: this._computeSMS('B'),
        RMS: this._computeRMS('B'),
        PMS: this._computePMS('B'),
        GPS: this._computeGPS('B')
      }
    };
  }

  _computeRecencyLayer(player) {
    if (this.snapshots.length === 0) return 50;
    const currentPt = this.totalPointsPlayed;
    let weightSum = 0;
    let valSum = 0;
    for (const snap of this.snapshots) {
      const age = currentPt - snap.pointIndex;
      const w = Math.exp(-LN2 * age / this.lambda);
      const val = player === 'A' ? snap.compositeA : snap.compositeB;
      valSum += w * val;
      weightSum += w;
    }
    return weightSum > 0 ? valSum / weightSum : 50;
  }

  _computeSetLayer(player) {
    if (this.setSnapshots.length === 0) {
      // Use carryover from previous set if available
      if (this.setLayerInitA !== null && player === 'A') return this.setLayerInitA;
      if (this.setLayerInitB !== null && player === 'B') return this.setLayerInitB;
      return 50;
    }
    const vals = this.setSnapshots.map(s => player === 'A' ? s.compositeA : s.compositeB);
    let mean = vals.reduce((a, b) => a + b, 0) / vals.length;

    // Apply carryover initialization
    if (this.setLayerInitA !== null && player === 'A' && this.setSnapshots.length < 5) {
      // Blend carryover with current data, fading as more data accumulates
      const carryoverWeight = Math.max(0, 1 - this.setSnapshots.length / 5);
      mean = carryoverWeight * this.setLayerInitA + (1 - carryoverWeight) * mean;
    }
    if (this.setLayerInitB !== null && player === 'B' && this.setSnapshots.length < 5) {
      const carryoverWeight = Math.max(0, 1 - this.setSnapshots.length / 5);
      mean = carryoverWeight * this.setLayerInitB + (1 - carryoverWeight) * mean;
    }

    return mean;
  }

  _computeGameProb(pHatA, pHatB) {
    // P(A wins current game) depends on who is serving
    const serverP = this.currentServer === 'A' ? pHatA : (1 - pHatB);
    // serverP = P(point goes to current server's advantage)
    // If A serves, P(A wins point) = pHatA
    // If B serves, P(A wins point) = 1 - pHatB

    if (this.isTiebreak) {
      // In tiebreak, approximate from current point score
      const pA = this.currentServer === 'A' ? pHatA : (1 - pHatB);
      // Simplified: use average serve prob for tiebreak
      const avgP = (pHatA + (1 - pHatB)) / 2;
      return gameWinProbFromScore(avgP, this.pointsA, this.pointsB, false, null);
    }

    return gameWinProbFromScore(serverP, this.pointsA, this.pointsB, this.isDeuce, this.isAdvantage);
  }

  _computeSetProb(pHatA, pHatB) {
    const gA = G(pHatA);        // P(A holds serve)
    const hA = 1 - G(pHatB);   // P(A breaks B)
    const tieP = G((pHatA + (1 - pHatB)) / 2); // tiebreak approx
    return setWinProb(gA, hA, this.gamesA, this.gamesB, tieP);
  }

  _computeMatchProb(pHatA, pHatB, setP) {
    return matchWinProb(setP, this.setsA, this.setsB);
  }

  getState() {
    const signals = this._computeAllSignals();
    const compositeA = (signals.A.NMI + signals.A.SMS + signals.A.RMS + signals.A.PMS + signals.A.GPS) / 5;
    const compositeB = (signals.B.NMI + signals.B.SMS + signals.B.RMS + signals.B.PMS + signals.B.GPS) / 5;

    const recA = this._computeRecencyLayer('A');
    const recB = this._computeRecencyLayer('B');
    const setA = this._computeSetLayer('A');
    const setB = this._computeSetLayer('B');

    const D_A = clamp(this.w_r * recA + this.w_s * setA, 0, 100);
    const D_B = clamp(this.w_r * recB + this.w_s * setB, 0, 100);
    const delta = D_A - D_B;

    const pHatA = clamp(this.p0A + this.k * (2 * sigmoid(delta / this.sigma) - 1), 0.35, 0.85);
    const pHatB = clamp(this.p0B + this.k * (2 * sigmoid(-delta / this.sigma) - 1), 0.35, 0.85);

    const gameProb = this._computeGameProb(pHatA, pHatB);
    const setProb = this._computeSetProb(pHatA, pHatB);
    const matchProb = this._computeMatchProb(pHatA, pHatB, setProb);

    const pointLabels = ['0', '15', '30', '40'];
    const formatPoint = (pts, player) => {
      if (this.isTiebreak) return String(pts);
      if (this.isAdvantage === player) return 'Ad';
      if (this.isDeuce) return '40';
      if (pts < 4) return pointLabels[pts];
      return 'Game';
    };

    return {
      score: {
        setsA: this.setsA,
        setsB: this.setsB,
        gamesA: this.gamesA,
        gamesB: this.gamesB,
        pointsA: formatPoint(this.pointsA, 'A'),
        pointsB: formatPoint(this.pointsB, 'B'),
        server: this.currentServer,
        isTiebreak: this.isTiebreak,
        pointNumber: this.totalPointsPlayed
      },
      signals,
      dominance: {
        D_A,
        D_B,
        delta,
        recencyLayer_A: recA,
        recencyLayer_B: recB,
        setLayer_A: setA,
        setLayer_B: setB
      },
      pHat: { A: pHatA, B: pHatB },
      probabilities: {
        gameA: gameProb,
        setA: setProb,
        matchA: matchProb
      },
      history: this.pointHistory.slice(0, 30)
    };
  }
}
