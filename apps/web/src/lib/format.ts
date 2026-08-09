export const money = (v: number | null | undefined, dp = 0): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const sign = v < 0 ? '-' : ''
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  })}`
}

export const signedMoney = (v: number | null | undefined, dp = 0): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const s = money(Math.abs(v), dp)
  return v < 0 ? `-${s}` : `+${s}`
}

export const pct = (v: number | null | undefined, dp = 1): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(dp)}%`

export const num = (v: number | null | undefined, dp = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(dp)

export const compact = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e12) return `${sign}${(abs / 1e12).toFixed(2)}T`
  if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}K`
  return `${sign}${abs.toFixed(0)}`
}

/** Nice axis ticks: 1/2/5 x 10^n, covering [lo, hi]. */
export function ticks(lo: number, hi: number, target = 6): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return [lo]
  const raw = (hi - lo) / target
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag
  const out: number[] = []
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) {
    out.push(Math.abs(t) < step * 1e-9 ? 0 : t)
  }
  return out
}

export const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

/** Index of the array element nearest to `value` (array must be ascending). */
export function nearestIndex(arr: number[], value: number): number {
  if (arr.length === 0) return 0
  let lo = 0
  let hi = arr.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (arr[mid] <= value) lo = mid
    else hi = mid
  }
  return Math.abs(arr[lo] - value) <= Math.abs(arr[hi] - value) ? lo : hi
}
