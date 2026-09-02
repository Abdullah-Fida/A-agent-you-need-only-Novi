import { MetadataRoute } from 'next';
import { getArticles, getCategories } from '@/lib/supabase';
import { SITE_URL, AUTHOR_SLUG } from '@/lib/site';

export const revalidate = 3600;

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const routes: MetadataRoute.Sitemap = [
    {
      url: SITE_URL,
      lastModified: new Date(),
      changeFrequency: 'hourly',
      priority: 1.0,
    },
    {
      // Editorial standards page — a trust signal search engines look for
      url: `${SITE_URL}/about`,
      lastModified: new Date(),
      changeFrequency: 'yearly',
      priority: 0.4,
    },
    {
      // The byline's bio. Google reads this alongside every article's
      // author field to decide whether the writing is worth trusting on
      // money and crypto topics, so it has to be crawlable in its own right.
      url: `${SITE_URL}/author/${AUTHOR_SLUG}`,
      lastModified: new Date(),
      changeFrequency: 'weekly',
      priority: 0.5,
    },
    {
      url: `${SITE_URL}/privacy`,
      lastModified: new Date(),
      changeFrequency: 'yearly',
      priority: 0.3,
    },
  ];

  const [articles, categories] = await Promise.all([
    getArticles(1000),
    getCategories(),
  ]);

  for (const category of categories) {
    routes.push({
      url: `${SITE_URL}/category/${encodeURIComponent(category)}`,
      lastModified: new Date(),
      changeFrequency: 'daily',
      priority: 0.6,
    });
  }

  for (const article of articles) {
    const published = new Date(article.published_at);
    const ageDays = (Date.now() - published.getTime()) / 86_400_000;
    routes.push({
      url: `${SITE_URL}/${article.slug}`,
      lastModified: published,
      // Fresh news changes often; older pieces settle down.
      changeFrequency: ageDays < 2 ? 'hourly' : ageDays < 14 ? 'daily' : 'monthly',
      priority: ageDays < 2 ? 0.9 : ageDays < 14 ? 0.7 : 0.5,
    });
  }

  return routes;
}
