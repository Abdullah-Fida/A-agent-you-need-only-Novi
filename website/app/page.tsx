import Link from 'next/link';
import type { Metadata } from 'next';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticles, Article } from '@/lib/supabase';

export const revalidate = 60;

export const metadata: Metadata = {
  alternates: { canonical: '/' },
};

/** How many sections get their own block on the front page. */
const FRONT_SECTIONS = 4;
/** Stories shown under each section heading. */
const PER_SECTION = 3;

function formatDate(value: string): string {
  try {
    return new Date(value).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
    });
  } catch {
    return '';
  }
}

/**
 * A story is only front-page material if it has a picture.
 *
 * A card with an empty image well is the single thing that makes a news site
 * look broken, and it is worse than the story simply not appearing — the
 * article is still reachable from its section page and from search.
 */
function hasImage(a: Article): boolean {
  return Boolean((a.main_image_url || '').trim());
}

export default async function HomePage() {
  const all = await getArticles(60);
  const articles = all.filter(hasImage);

  if (articles.length === 0) {
    // Deliberately says nothing about tables, environment variables or which
    // agent is switched off. That belongs in the logs, not in front of a
    // reader who wandered in from search.
    return (
      <>
        <Navbar />
        <main className="main-container">
          <section className="empty">
            <h1>Nothing published yet</h1>
            <p>New reporting appears here as soon as it is filed. Please check back shortly.</p>
            <Link href="/about" className="empty-link">About this publication</Link>
          </section>
        </main>
        <Footer />
      </>
    );
  }

  const [lead, ...rest] = articles;
  const rail = rest.slice(0, 4);
  const remainder = rest.slice(4);

  // Group what is left by section, in order of how much each section has, so
  // the front page leads with whatever the newsroom actually covered today
  // rather than with a fixed running order.
  const bySection = new Map<string, Article[]>();
  for (const a of remainder) {
    const key = a.category || 'Latest';
    if (!bySection.has(key)) bySection.set(key, []);
    bySection.get(key)!.push(a);
  }
  const sections = [...bySection.entries()]
    .filter(([, items]) => items.length >= 2)
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, FRONT_SECTIONS);

  const featured = new Set(
    sections.flatMap(([, items]) => items.slice(0, PER_SECTION).map((a) => a.slug)),
  );
  const more = remainder.filter((a) => !featured.has(a.slug)).slice(0, 6);

  return (
    <>
      <Navbar />
      <main className="main-container">
        <section className="lead-grid">
          <Link href={`/${lead.slug}`} className="lead">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={lead.main_image_url} alt="" className="lead-img" />
            <div className="lead-body">
              <span className="tag">{lead.category}</span>
              <h1>{lead.title}</h1>
              <p>{lead.summary}</p>
              <div className="byline">
                <time dateTime={lead.published_at}>{formatDate(lead.published_at)}</time>
                {lead.reading_minutes ? (
                  <><span aria-hidden="true">·</span><span>{lead.reading_minutes} min read</span></>
                ) : null}
              </div>
            </div>
          </Link>

          <aside className="rail" aria-label="Also in the news">
            <h2 className="rail-title">Also in the news</h2>
            {rail.map((a: Article) => (
              <Link key={a.slug} href={`/${a.slug}`} className="rail-item">
                <span className="tag tag--quiet">{a.category}</span>
                <h3>{a.title}</h3>
                <time dateTime={a.published_at}>{formatDate(a.published_at)}</time>
              </Link>
            ))}
          </aside>
        </section>

        {sections.map(([name, items]) => (
          <section key={name} className="section-block">
            <div className="section-head">
              <h2>{name}</h2>
              <Link href={`/category/${encodeURIComponent(name)}`}>
                All {name}
                <span aria-hidden="true"> →</span>
              </Link>
            </div>
            <div className="grid grid--ruled">
              {items.slice(0, PER_SECTION).map((a: Article) => (
                <ArticleCard key={a.slug} article={a} showImage />
              ))}
            </div>
          </section>
        ))}

        {more.length > 0 && (
          <section className="section-block">
            <div className="section-head">
              <h2>More stories</h2>
            </div>
            <ul className="headline-list">
              {more.map((a: Article) => (
                <li key={a.slug}>
                  <Link href={`/${a.slug}`}>
                    <span className="tag tag--quiet">{a.category}</span>
                    <h3>{a.title}</h3>
                    <time dateTime={a.published_at}>{formatDate(a.published_at)}</time>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        )}
      </main>
      <Footer />
    </>
  );
}
