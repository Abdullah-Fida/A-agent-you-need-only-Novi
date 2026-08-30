/**
 * Where this site lives, resolved once for the whole app.
 *
 * Every canonical URL, sitemap entry, RSS link and OpenGraph tag is built
 * from SITE_URL, so a wrong value here silently tells Google the content
 * belongs to another domain. It previously fell back to a hardcoded
 * placeholder domain, which is exactly what shipped whenever the
 * environment variable failed to reach the build.
 *
 * Order:
 *   1. NEXT_PUBLIC_SITE_URL      — set this once you have a real domain
 *   2. VERCEL_PROJECT_PRODUCTION_URL — injected by Vercel automatically and
 *      stable across deployments, so the site is correct with no config
 *   3. localhost                 — local development
 *
 * There is deliberately no hardcoded public domain in this chain: an unset
 * variable should degrade to *this* deployment, never to somebody else's site.
 */
function resolveSiteUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_SITE_URL?.trim();
  if (explicit) return explicit.replace(/\/+$/, '');

  // Vercel sets this to the project's production domain (no deployment hash).
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL?.trim();
  if (vercel) return `https://${vercel.replace(/\/+$/, '')}`;

  return 'http://localhost:3000';
}

export const SITE_URL = resolveSiteUrl();
export const SITE_NAME = process.env.NEXT_PUBLIC_SITE_NAME?.trim() || 'PressVane';
export const TELEGRAM_URL =
  process.env.NEXT_PUBLIC_TELEGRAM_URL?.trim() || 'https://t.me/Novi_Network';

export const SITE_DESCRIPTION =
  'Independent coverage of world affairs, crypto and Web3, technology, business ' +
  'and South Asia — published continuously, with the context behind each story.';
