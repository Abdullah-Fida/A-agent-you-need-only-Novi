import Link from 'next/link';
import type { Metadata } from 'next';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticles, getCategories, Article } from '@/lib/supabase';

export const revalidate = 60;

const SITE_NAME = process.env.NEXT_PUBLIC_SITE_NAME || 'Novi News';

export const metadata: Metadata = {
  alternates: { canonical: '/' },
};

function formatDate(value: string): string {
  try {
    return new Date(value).toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
    });
  } catch {
    return '';
  }
}

export default async function HomePage() {
  const [articles, categories] = await Promise.all([getArticles(25), getCategories()]);

  // No mock fallback: silently showing invented articles hid the fact that
  // the database was empty and the pipeline was broken.
  if (articles.length === 0) {
    return (
      <>
        <Navbar />
        <main className="main-container">
          <section className="empty">
            <h1>No stories published yet</h1>
            <p>
              {SITE_NAME} publishes automatically as soon as the news agent runs.
              If this persists, confirm the <code>articles</code> table exists
              (run <code>database/schema.sql</code>) and that the news agent is
              switched on in the NOVI dashboard.
            </p>
          </section>
        </main>
        <Footer />
      </>
    );
  }

  const [lead, ...rest] = articles;
  const secondary = rest.slice(0, 4);
  const remainder = rest.slice(4);

  return (
    <>
      <Navbar />
      <main className="main-container">
        <section className="lead-grid">
          <Link href={`/${lead.slug}`} className="lead">
            {lead.main_image_url && (
              /* eslint-disable-next-line @next/next/no-img-element */
              <img src={lead.main_image_url} alt={lead.title} className="lead-img" />
            )}
            <div className="lead-body">
              <span className="tag">{lead.category}</span>
              <h1>{lead.title}</h1>
              <p>{lead.summary}</p>
              <div className="byline">
                <time dateTime={lead.published_at}>{formatDate(lead.published_at)}</time>
                {lead.reading_minutes ? <><span aria-hidden="true">·</span><span>{lead.reading_minutes} min read</span></> : null}
              </div>
            </div>
          </Link>

          <div className="secondary">
            {secondary.map((a: Article) => (
              <Link key={a.slug} href={`/${a.slug}`} className="secondary-item">
                <span className="tag">{a.category}</span>
                <h3>{a.title}</h3>
                <time dateTime={a.published_at}>{formatDate(a.published_at)}</time>
              </Link>
            ))}
          </div>
        </section>

        {categories.length > 0 && (
          <nav className="cat-strip" aria-label="Categories">
            {categories.map((c) => (
              <Link key={c} href={`/category/${encodeURIComponent(c)}`}>{c}</Link>
            ))}
          </nav>
        )}

        {remainder.length > 0 && (
          <section>
            <h2 className="section-title">Latest</h2>
            <div className="grid">
              {remainder.map((a: Article) => (
                <ArticleCard key={a.slug} article={a} />
              ))}
            </div>
          </section>
        )}
      </main>
      <Footer />
    </>
  );
}
