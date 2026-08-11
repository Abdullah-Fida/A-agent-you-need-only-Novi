import { Metadata } from 'next';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import Image from 'next/image';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import { supabase, Article } from '@/lib/supabase';
import { SITE_URL, SITE_NAME } from '@/lib/site';

interface ArticlePageProps {
  params: Promise<{ slug: string }>;
}

export const revalidate = 60;

async function getArticleBySlug(slug: string): Promise<Article | null> {
  if (!supabase) return null;
  try {
    const { data, error } = await supabase
      .from('articles')
      .select('*')
      .eq('slug', slug)
      .eq('status', 'published')
      .maybeSingle();

    if (error || !data) return null;
    return data as Article;
  } catch {
    return null;
  }
}

async function getRelated(category: string, slug: string): Promise<Article[]> {
  if (!supabase) return [];
  try {
    const { data } = await supabase
      .from('articles')
      .select('*')
      .eq('category', category)
      .eq('status', 'published')
      .neq('slug', slug)
      .order('published_at', { ascending: false })
      .limit(3);
    return (data as Article[]) || [];
  } catch {
    return [];
  }
}

/** Per-article metadata — this is what search engines and social previews read. */
export async function generateMetadata({ params }: ArticlePageProps): Promise<Metadata> {
  const { slug } = await params;
  const article = await getArticleBySlug(slug);

  if (!article) {
    return { title: 'Article not found', robots: { index: false, follow: false } };
  }

  const title = article.meta_title || article.title;
  const description = article.meta_description || article.summary || '';
  const url = `${SITE_URL}/${article.slug}`;
  const images = article.main_image_url ? [{ url: article.main_image_url }] : [];

  return {
    title,
    description,
    keywords: article.seo_keywords || [],
    alternates: { canonical: url },
    openGraph: {
      type: 'article',
      title,
      description,
      url,
      siteName: SITE_NAME,
      publishedTime: article.published_at,
      authors: [article.author || SITE_NAME],
      section: article.category,
      tags: article.seo_keywords || [],
      images,
    },
    twitter: {
      card: 'summary_large_image',
      title,
      description,
      images: article.main_image_url ? [article.main_image_url] : [],
    },
  };
}

function formatDate(value: string): string {
  try {
    return new Date(value).toLocaleDateString('en-US', {
      year: 'numeric', month: 'long', day: 'numeric',
    });
  } catch {
    return '';
  }
}

export default async function ArticlePage({ params }: ArticlePageProps) {
  const { slug } = await params;
  const article = await getArticleBySlug(slug);

  if (!article) notFound();

  const related = await getRelated(article.category, article.slug);
  const published = formatDate(article.published_at);

  // NewsArticle structured data — required for Google News / Top Stories.
  // headline must stay under 110 characters or Google drops the rich result.
  const articleSchema = {
    '@context': 'https://schema.org',
    '@type': 'NewsArticle',
    '@id': `${SITE_URL}/${article.slug}#article`,
    headline: article.title.slice(0, 110),
    description: article.meta_description || article.summary,
    image: article.main_image_url
      ? [article.main_image_url]
      : [`${SITE_URL}/og-default.png`],
    datePublished: article.published_at,
    dateModified: article.created_at || article.published_at,
    author: {
      '@type': 'Organization',
      name: article.author || SITE_NAME,
      url: `${SITE_URL}/about`,
    },
    publisher: { '@id': `${SITE_URL}/#organization` },
    mainEntityOfPage: { '@type': 'WebPage', '@id': `${SITE_URL}/${article.slug}` },
    articleSection: article.category,
    keywords: (article.seo_keywords || []).join(', '),
    wordCount: article.word_count || undefined,
    timeRequired: article.reading_minutes ? `PT${article.reading_minutes}M` : undefined,
    inLanguage: 'en',
    isAccessibleForFree: true,
    ...(article.source_url && article.source_name
      ? { citation: { '@type': 'CreativeWork', name: article.source_name, url: article.source_url } }
      : {}),
  };

  const breadcrumbSchema = {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: [
      { '@type': 'ListItem', position: 1, name: 'Home', item: SITE_URL },
      {
        '@type': 'ListItem', position: 2, name: article.category,
        item: `${SITE_URL}/category/${encodeURIComponent(article.category)}`,
      },
      { '@type': 'ListItem', position: 3, name: article.title },
    ],
  };

  return (
    <>
      <script type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(articleSchema) }} />
      <script type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(breadcrumbSchema) }} />

      <Navbar />

      <main className="main-container">
        <article className="article">
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link href="/">Home</Link>
            <span aria-hidden="true">/</span>
            <Link href={`/category/${encodeURIComponent(article.category)}`}>
              {article.category}
            </Link>
          </nav>

          <header className="article-head">
            <span className="tag">{article.category}</span>
            <h1>{article.title}</h1>
            {article.summary && <p className="standfirst">{article.summary}</p>}
            <div className="byline">
              <span>{article.author || SITE_NAME}</span>
              {published && <><span aria-hidden="true">·</span><time dateTime={article.published_at}>{published}</time></>}
              {article.reading_minutes ? (
                <><span aria-hidden="true">·</span><span>{article.reading_minutes} min read</span></>
              ) : null}
            </div>
          </header>

          {article.main_image_url && (
            // This is the page's Largest Contentful Paint element, so it is
            // preloaded from <head> rather than discovered in <body>.
            // (`priority` was deprecated in Next.js 16 in favour of `preload`.)
            <Image
              className="hero"
              src={article.main_image_url}
              alt={article.title}
              width={1200}
              height={675}
              sizes="(max-width: 780px) 100vw, 720px"
              preload
            />
          )}

          <div
            className="prose"
            dangerouslySetInnerHTML={{ __html: article.content }}
          />

          {article.seo_keywords?.length > 0 && (
            <ul className="tags" aria-label="Topics">
              {article.seo_keywords.slice(0, 8).map((k) => (
                <li key={k}>{k}</li>
              ))}
            </ul>
          )}

          {article.source_name && (
            <p className="source">
              Reporting informed by{' '}
              {article.source_url
                ? <a href={article.source_url} rel="nofollow noopener" target="_blank">{article.source_name}</a>
                : article.source_name}
            </p>
          )}
        </article>

        {related.length > 0 && (
          <section className="related">
            <h2>More in {article.category}</h2>
            <div className="related-grid">
              {related.map((r) => (
                <Link key={r.slug} href={`/${r.slug}`} className="related-card">
                  <span className="tag">{r.category}</span>
                  <h3>{r.title}</h3>
                  <time dateTime={r.published_at}>{formatDate(r.published_at)}</time>
                </Link>
              ))}
            </div>
          </section>
        )}
      </main>

      <Footer />
    </>
  );
}
