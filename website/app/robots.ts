import { MetadataRoute } from 'next';
import { SITE_URL } from '@/lib/site';

/**
 * robots.txt
 *
 * The base URL comes from lib/site, never from a local fallback. This file
 * used to default to a hardcoded placeholder domain, so an unset
 * NEXT_PUBLIC_SITE_URL published a robots.txt pointing Google at somebody
 * else's sitemap -- the exact failure lib/site exists to prevent.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
        // /api/ is internal; the Next.js build output is not content.
        disallow: ['/api/', '/_next/'],
      },
    ],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
