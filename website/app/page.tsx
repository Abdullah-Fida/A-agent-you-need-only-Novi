import Link from 'next/link';
import Image from 'next/image';
import type { Metadata } from 'next';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticles, Article } from '@/lib/supabase';

export const revalidate = 60;

export const metadata: Metadata = {
  alternates: { canonical: '/' },
};

const FRONT_SECTIONS = 4;
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
 * "3 hours ago" rather than a date, for anything published today.
 *
 * Recency is the whole proposition of a news site, and a bare date hides it:
 * a story from this morning and one from this evening look identical.
 */
function timeAgo(value: string): string {
  try {
    const then = new Date(value).getTime();
    const mins = Math.floor((Date.now() - then) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours} hr${hours === 1 ? '' : 's'} ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
    return formatDate(value);
  } catch {
    return '';
  }
}

/** A story is only front-page material if it has a picture. */
function hasImage(a: Article): boolean {
  return Boolean((a.main_image_url || '').trim());
}

export default async function HomePage() {
  const all = await getArticles(60);
  const articles = all.filter(hasImage);

  if (articles.length === 0) {
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
  const underLead = rest.slice(0, 2);      // thumbnails beneath the lead
  const topStories = rest.slice(2, 7);     // the numbered rail
  const remainder = rest.slice(7);
  const ticker = articles.slice(0, 6);

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
  const more = remainder.filter((a) => !featured.has(a.slug)).slice(0, 8);

  return (
    <>
      <Navbar />

      {/* The wire strip. Every newsroom front page opens with one, and it is
          what tells a reader at a glance that the site is alive. */}
      <div className="ticker" aria-label="Latest headlines">
        <div className="main-container ticker-inner">
          <span className="ticker-flag">Latest</span>
          <div className="ticker-track">
            {ticker.map((a) => (
              <Link key={a.slug} href={`/${a.slug}`}>
                <span className="ticker-dot" aria-hidden="true" />
                {a.title}
              </Link>
            ))}
          </div>
        </div>
      </div>

      <main className="main-container">
        <section className="front">
          <div className="front-main">
            <Link href={`/${lead.slug}`} className="lead">
              <div className="lead-imgwrap">
                {/*
                  The most-requested asset on the site. As a plain <img> it
                  bypassed the optimizer completely: a full-size JPEG served
                  straight from Supabase on every single homepage view, with
                  no CDN cache and no AVIF conversion. Through next/image it
                  is resized, converted and cached at the edge, which is what
                  keeps Supabase egress flat as traffic grows.

                  priority, because this is the largest-contentful-paint
                  element and Core Web Vitals is a ranking signal.
                */}
                <Image
                  src={lead.main_image_url}
                  alt=""
                  className="lead-img"
                  width={1200}
                  height={675}
                  sizes="(max-width: 980px) 100vw, 66vw"
                  priority
                />
                <span className={`chip chip--${(lead.category || 'news').toLowerCase()}`}>
                  {lead.category}
                </span>
              </div>
              <div className="lead-body">
                <h1>{lead.title}</h1>
                <p>{lead.summary}</p>
                <div className="byline">
                  <time dateTime={lead.published_at}>{timeAgo(lead.published_at)}</time>
                  {lead.reading_minutes ? (
                    <><span aria-hidden="true">·</span><span>{lead.reading_minutes} min read</span></>
                  ) : null}
                  {lead.source_name ? (
                    <><span aria-hidden="true">·</span><span>{lead.source_name}</span></>
                  ) : null}
                </div>
              </div>
            </Link>

            <div className="under-lead">
              {underLead.map((a) => (
                <Link key={a.slug} href={`/${a.slug}`} className="mini">
                  <Image src={a.main_image_url} alt="" width={220} height={150}
                         className="mini-img" sizes="220px" />
                  <div>
                    <span className={`chip chip--${(a.category || 'news').toLowerCase()} chip--sm`}>
                      {a.category}
                    </span>
                    <h3>{a.title}</h3>
                    <time dateTime={a.published_at}>{timeAgo(a.published_at)}</time>
                  </div>
                </Link>
              ))}
            </div>
          </div>

          {/* Ranked by recency and labelled as such. There is no view data
              yet, and a "Most read" list built from nothing is a fiction. */}
          <aside className="rail" aria-label="Top stories">
            <h2 className="rail-title">Top stories</h2>
            <ol className="ranked">
              {topStories.map((a, i) => (
                <li key={a.slug}>
                  <Link href={`/${a.slug}`}>
                    <span className="rank" aria-hidden="true">{i + 1}</span>
                    <div>
                      <h3>{a.title}</h3>
                      <time dateTime={a.published_at}>{timeAgo(a.published_at)}</time>
                    </div>
                  </Link>
                </li>
              ))}
            </ol>
          </aside>
        </section>

        {sections.map(([name, items]) => (
          <section key={name} className="section-block">
            <div className={`section-head section-head--${name.toLowerCase()}`}>
              <h2>{name}</h2>
              <Link href={`/category/${encodeURIComponent(name)}`}>
                All {name}<span aria-hidden="true"> →</span>
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
                    <span className={`chip chip--${(a.category || 'news').toLowerCase()} chip--sm`}>
                      {a.category}
                    </span>
                    <h3>{a.title}</h3>
                    <time dateTime={a.published_at}>{timeAgo(a.published_at)}</time>
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
