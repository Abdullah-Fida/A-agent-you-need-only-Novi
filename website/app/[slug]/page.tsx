import { Metadata } from 'next';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import Image from 'next/image';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import { supabase, Article } from '@/lib/supabase';
import {
  SITE_URL, SITE_NAME, AUTHOR_NAME, AUTHOR_ROLE, AUTHOR_SLUG, AUTHOR_URL,
  LEGACY_BYLINES,
} from '@/lib/site';

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
    // Only a real person gets a profile URL. Pointing the old newsroom
    // byline at a Person page would claim someone wrote articles they did not.
    authors: [
      article.author === AUTHOR_NAME
        ? { name: AUTHOR_NAME, url: AUTHOR_URL }
        : { name: article.author || SITE_NAME },
    ],
    alternates: { canonical: url },
    openGraph: {
      type: 'article',
      title,
      description,
      url,
      siteName: SITE_NAME,
      publishedTime: article.published_at,
      // OpenGraph takes plain names here; the linked form is the
      // top-level `authors` field below.
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

  // Older articles carry a newsroom byline under two different spellings.
  // Those are not a person and must not be dressed up as one.
  const rawByline = article.author || SITE_NAME;
  const byline = {
    name: rawByline,
    isPerson: rawByline === AUTHOR_NAME && !LEGACY_BYLINES.includes(rawByline),
  };

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
    // A named Person, not the masthead. Google treats crypto, investing
    // and business as "Your Money or Your Life" topics and ranks an
    // anonymous publisher down however good the writing is; the byline has
    // to resolve to someone with a bio explaining why they are worth
    // reading. Articles filed under the old newsroom byline stay an
    // Organization, because claiming a person wrote them would be false.
    author: byline.isPerson
      ? {
          '@type': 'Person',
          '@id': `${AUTHOR_URL}#person`,
          name: byline.name,
          url: AUTHOR_URL,
          jobTitle: AUTHOR_ROLE,
        }
      : {
          '@type': 'Organization',
          name: byline.name,
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
        {/*
          Two columns on wide screens. The text stays at a reading measure —
          widening it past ~70 characters measurably hurts comprehension — so
          the space beside it carries related stories instead of sitting empty,
          which also keeps readers moving between articles.
        */}
        <div className="article-layout">
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
              {byline.isPerson ? (
                <span>
                  By <Link href={`/author/${AUTHOR_SLUG}`} rel="author">{byline.name}</Link>
                </span>
              ) : (
                <span>{byline.name}</span>
              )}
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
              // Describes the picture in context rather than repeating the
              // headline verbatim, which is what a screen reader announced
              // immediately after reading the same words as the <h1>.
              alt={`${article.category} — ${article.title}`}
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
            <aside className="article-rail" aria-label={`More in ${article.category}`}>
              <div className="rail-sticky">
                <h2>More in {article.category}</h2>
                <div className="rail-list">
                  {related.map((r) => (
                    <Link key={r.slug} href={`/${r.slug}`} className="rail-card">
                      <span className="tag">{r.category}</span>
                      <h3>{r.title}</h3>
                      <time dateTime={r.published_at}>{formatDate(r.published_at)}</time>
                    </Link>
                  ))}
                </div>
              </div>
            </aside>
          )}
        </div>
      </main>

      <Footer />
    </>
  );
}
