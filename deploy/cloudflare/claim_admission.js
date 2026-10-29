// Ephemeral scheduling hints, never lease or authorization authority.
// Only new claims wait here; heartbeats and submissions bypass this controller.
export class ClaimPressure extends Error {
  constructor(reason, retryAfter) {
    super(`claim pressure: ${reason}; retry with jitter`);
    this.retryAfter = retryAfter;
  }
}

const integer = (env, key, fallback) => {
  const value = env[key] === undefined ? fallback : Number(env[key]);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`invalid ${key}`);
  return value;
};

export function claimAdmissionOptions(env = {}) {
  const options = {
    initial: integer(env, "CLAIM_INITIAL_CONCURRENCY", 32),
    maximum: integer(env, "CLAIM_MAX_CONCURRENCY", 1024),
    maxPending: integer(env, "CLAIM_MAX_PENDING", 2048),
    maxWaitMs: integer(env, "CLAIM_MAX_WAIT_MS", 10000),
    targetMs: integer(env, "CLAIM_TARGET_MS", 500),
  };
  if (options.initial > options.maximum) throw new Error("claim initial concurrency exceeds maximum");
  return options;
}

export class ClaimAdmission {
  constructor(options = {}, clock = () => Date.now()) {
    Object.assign(this, claimAdmissionOptions(), options);
    this.clock = clock;
    this.limit = this.initial;
    this.active = 0;
    this.pending = 0;
    this.queues = new Map();
    this.ewmaMs = null;
    this.lastAdjustment = -Infinity;
  }

  retryAfter() {
    return Math.max(1, Math.ceil((this.ewmaMs || this.targetMs) *
      Math.ceil((this.pending + 1) / this.limit) / 1000));
  }

  acquire(principal) {
    const arrived = this.clock();
    if (this.active < this.limit && this.pending === 0) return Promise.resolve(this.start(arrived));
    if (this.pending >= this.maxPending) return Promise.reject(new ClaimPressure("pending queue full", this.retryAfter()));
    return new Promise((resolve, reject) => {
      const entry = { arrived, resolve, reject };
      const queue = this.queues.get(principal) || [];
      queue.push(entry);
      this.queues.set(principal, queue);
      this.pending++;
      entry.timer = setTimeout(() => {
        const current = this.queues.get(principal);
        const index = current?.indexOf(entry) ?? -1;
        if (index < 0) return;
        current.splice(index, 1);
        if (!current.length) this.queues.delete(principal);
        this.pending--;
        reject(new ClaimPressure("pacing deadline reached", this.retryAfter()));
        this.drain();
      }, this.maxWaitMs);
      this.drain();
    });
  }

  start(arrived) {
    this.active++;
    const started = this.clock();
    const saturated = this.active >= this.limit;
    let released = false;
    return {
      waitMs: Math.max(0, started - arrived),
      release: () => {
        if (released) return;
        released = true;
        this.active--;
        const elapsed = Math.max(0, this.clock() - started);
        this.ewmaMs = this.ewmaMs === null ? elapsed : this.ewmaMs * 0.8 + elapsed * 0.2;
        if (this.clock() - this.lastAdjustment >= this.targetMs) {
          if (this.ewmaMs > this.targetMs) {
            this.limit = Math.max(1, Math.floor(this.limit * 0.7));
            this.lastAdjustment = this.clock();
          } else if (this.ewmaMs < this.targetMs * 0.8 && (saturated || this.pending > 0)) {
            this.limit = Math.min(this.maximum, this.limit + 1);
            this.lastAdjustment = this.clock();
          }
        }
        this.drain();
      },
    };
  }

  drain() {
    while (this.active < this.limit && this.pending > 0) {
      // Rotate principals, preserving FIFO within each principal.
      const [principal, queue] = this.queues.entries().next().value;
      const entry = queue.shift();
      this.queues.delete(principal);
      if (queue.length) this.queues.set(principal, queue);
      this.pending--;
      clearTimeout(entry.timer);
      if (this.clock() - entry.arrived >= this.maxWaitMs) {
        entry.reject(new ClaimPressure("pacing deadline reached", this.retryAfter()));
      } else entry.resolve(this.start(entry.arrived));
    }
  }
}

// Idle pacing for empty claims. The Retry-After hint doubles while one key keeps finding a
// queue empty and resets when that key is granted a lease or the queue receives new work.
// It reports observed emptiness; it is not a limit, and a worker may long-poll instead.
export const EMPTY_CLAIM_BASE_SECONDS = 5;
export const EMPTY_CLAIM_MAX_SECONDS = 60;
export function emptyClaimRetryAfter(consecutiveEmpty) {
  const n = Math.max(1, Math.floor(Number(consecutiveEmpty) || 1));
  return Math.min(EMPTY_CLAIM_MAX_SECONDS, EMPTY_CLAIM_BASE_SECONDS * 2 ** Math.min(n - 1, 16));
}

export class IdleClaims {
  constructor(maxQueues = 4096) { this.maxQueues = maxQueues; this.queues = new Map(); }
  empty(namespace, queue, keyId) {
    const name = `${namespace}\u0000${queue}`;
    let keys = this.queues.get(name);
    if (!keys) {
      if (this.queues.size >= this.maxQueues) this.queues.clear(); // memory bound; hints restart at the base
      keys = new Map();
      this.queues.set(name, keys);
    }
    const n = (keys.get(keyId) || 0) + 1;
    keys.set(keyId, n);
    return emptyClaimRetryAfter(n);
  }
  granted(namespace, queue, keyId) { this.queues.get(`${namespace}\u0000${queue}`)?.delete(keyId); }
  work(namespace, queue) { this.queues.delete(`${namespace}\u0000${queue}`); }
}
