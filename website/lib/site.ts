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

/**
 * The byline.
 *
 * Google's Search Quality Rater Guidelines treat finance, crypto and
 * investing as "Your Money or Your Life" topics, where an anonymous
 * publisher is ranked down however good the writing is. What they look for
 * is a named person, a bio that explains why that person is worth reading
 * on the subject, and a page where both live.
 *
 * Defaults are the real values rather than placeholders, so the site is
 * correct with no configuration; the environment variables exist so the
 * byline can change without a code edit.
 */
export const AUTHOR_NAME =
  process.env.NEXT_PUBLIC_AUTHOR_NAME?.trim() || 'Abdullah Fida';

export const AUTHOR_ROLE =
  process.env.NEXT_PUBLIC_AUTHOR_ROLE?.trim() || 'Founder & Editor';

/** Kept in sync with the Python side's slugify: lowercase, hyphenated. */
export const AUTHOR_SLUG = AUTHOR_NAME.toLowerCase()
  .replace(/[^a-z0-9]+/g, '-')
  .replace(/^-+|-+$/g, '');

export const AUTHOR_URL = `${SITE_URL}/author/${AUTHOR_SLUG}`;

export const AUTHOR_BIO: string[] = [
  `${AUTHOR_NAME} is the Founder and Editor of ${SITE_NAME}, an independent ` +
    'digital publication covering news, current affairs, technology, business ' +
    'and useful evergreen topics. He focuses on creating clear, informative and ' +
    'reader-friendly content that helps people understand important stories and ' +
    'discover practical information.',
  'As the founder and editor, Abdullah oversees the publication\u2019s content ' +
    'direction, editorial standards and publishing process, with an emphasis on ' +
    'accuracy, clarity and useful journalism.',
];

/**
 * Bylines used before the named byline existed. The author page still has to
 * claim those articles, or two thirds of the archive would show a byline
 * that leads nowhere. Two spellings because both were live.
 */
export const LEGACY_BYLINES = [
  'PressVane Newsroom',
  'Press Vane Newsroom',
  `${SITE_NAME} Newsroom`,
];
