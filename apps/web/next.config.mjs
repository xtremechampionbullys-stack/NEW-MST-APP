/** @type {import('next').NextConfig} */

/*
 * There is deliberately no `rewrites()` proxy here any more.
 *
 * `rewrites()` is evaluated during `next build` and its return value is baked
 * into the routes manifest, so `process.env.API_URL` read here would be frozen
 * at the build machine's value — setting it at deploy time would do nothing
 * while looking like it worked, and every call would quietly go to the default
 * localhost:8000. The proxy now lives in src/app/api/[...path]/route.ts, where
 * the variable is read per request.
 */
const nextConfig = {
  reactStrictMode: true,
}
export default nextConfig
