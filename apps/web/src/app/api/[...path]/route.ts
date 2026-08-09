/**
 * Runtime proxy to the FastAPI service.
 *
 * This replaces a `rewrites()` entry in next.config.mjs, and the reason is not
 * style. `rewrites()` runs once, during `next build`, and its return value is
 * serialised into the routes manifest. So `process.env.API_URL` was frozen at
 * whatever it was on the build machine — setting it before `next start`, or in
 * a container's environment, silently did nothing and every call proxied to
 * the default `localhost:8000`. That failure is invisible in dev (where the
 * default happens to be right) and shows up in production as a wall of 500s
 * with no explanation.
 *
 * A route handler reads the variable per request instead, so the deployed
 * container's environment is the one that applies.
 *
 * Headers are forwarded selectively. `host` must not be passed through — the
 * upstream would see the browser's host and, on any service that does
 * host-based routing, answer for the wrong app. Hop-by-hop headers are dropped
 * for the same reason a real proxy drops them.
 */

import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const UPSTREAM = () => process.env.API_URL || 'http://localhost:8000'

// Hop-by-hop headers (RFC 9110 §7.6.1) plus the ones Next sets for its own
// routing. Forwarding any of these corrupts the connection or the response.
const DROP = new Set([
  'host', 'connection', 'keep-alive', 'transfer-encoding', 'upgrade',
  'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
  'content-length', 'content-encoding',
])

async function proxy(req: NextRequest, path: string[]) {
  const url = new URL(req.url)
  const target = `${UPSTREAM()}/api/${path.map(encodeURIComponent).join('/')}${url.search}`

  const headers = new Headers()
  req.headers.forEach((v, k) => {
    if (!DROP.has(k.toLowerCase())) headers.set(k, v)
  })

  let res: Response
  try {
    res = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.arrayBuffer(),
      redirect: 'manual',
      cache: 'no-store',
    })
  } catch (err) {
    // A connection failure is the single most common thing to go wrong here and
    // the default message ("fetch failed") names neither end. Say which address
    // was tried, because that is the whole diagnosis.
    return Response.json(
      {
        detail: `Cannot reach the API at ${UPSTREAM()}. Start it, or set API_URL ` +
          `to where it is running. (${err instanceof Error ? err.message : String(err)})`,
        upstream: UPSTREAM(),
      },
      { status: 502 },
    )
  }

  const out = new Headers()
  res.headers.forEach((v, k) => {
    if (!DROP.has(k.toLowerCase())) out.set(k, v)
  })
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: out })
}

type Ctx = { params: Promise<{ path: string[] }> }

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
export async function PUT(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
export async function PATCH(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
export async function HEAD(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path)
}
