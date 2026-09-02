import { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticlesByAuthor, Article } from '@/lib/supabase';
import {
  SITE_URL, SITE_NAME, AUTHOR_NAME, AUTHOR_ROLE, AUTHOR_SLUG,
  AUTHOR_URL, AUTHOR_BIO, LEGACY_BYLINES,
} from '@/lib/site';

interface AuthorPageProps {
  params: Promise<{ slug: string }>;
}

export const revalidate = 300;

export async function generateStaticParams() {
  return [{ slug: AUTHOR_SLUG }];
}

export async function generateMetadata({ params }: AuthorPageProps): Promise<Metadata> {
  const { slug } = await params;
  if (slug !== AUTHOR_SLUG) return {};

  const description = `${AUTHOR_NAME} is ${AUTHOR_ROLE} of ${SITE_NAME}, ` +
    'covering world affairs, crypto, technology and business.';

  return {
    title: `${AUTHOR_NAME} — ${AUTHOR_ROLE}`,
    description,
    alternates: { canonical: AUTHOR_URL },
    openGraph: {
      type: 'profile',
      title: `${AUTHOR_NAME} — ${AUTHOR_ROLE}, ${SITE_NAME}`,
      description,
      url: AUTHOR_URL,
      siteName: SITE_NAME,
    },
  };
}

export default async function AuthorPage({ params }: AuthorPageProps) {
  const { slug } = await params;
  if (slug !== AUTHOR_SLUG) notFound();

  const articles = await getArticlesByAuthor([AUTHOR_NAME, ...LEGACY_BYLINES]);

  // The Person record every article's author field points at. Google reads
  // the byline and this page as one claim: who wrote it, and what makes
  // them worth reading on the subject.
  const personSchema = {
    '@context': 'https://schema.org',
    '@type': 'Person',
    '@id': `${AUTHOR_URL}#person`,
    name: AUTHOR_NAME,
    url: AUTHOR_URL,
    jobTitle: AUTHOR_ROLE,
    description: AUTHOR_BIO.join(' '),
    knowsAbout: [
      'World affairs', 'Cryptocurrency', 'Blockchain', 'Technology',
      'Business and markets', 'South Asia', 'Pakistan',
    ],
    worksFor: { '@type': 'Organization', name: SITE_NAME, url: SITE_URL },
    mainEntityOfPage: { '@type': 'ProfilePage', '@id': AUTHOR_URL },
  };

  const sections = Array.from(new Set(articles.map((a) => a.category).filter(Boolean)));

  return (
    <>
      <script type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(personSchema) }} />

      <Navbar />

      <main className="main-container">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <Link href="/">Home</Link>
          <span aria-hidden="true">/</span>
          <span>{AUTHOR_NAME}</span>
        </nav>

        <header className="author-head">
          <h1>{AUTHOR_NAME}</h1>
          <p className="author-role">{AUTHOR_ROLE}, {SITE_NAME}</p>
          {AUTHOR_BIO.map((para) => (
            <p key={para.slice(0, 40)} className="author-bio">{para}</p>
          ))}

          <dl className="author-facts">
            <div>
              <dt>Published</dt>
              <dd>{articles.length} article{articles.length === 1 ? '' : 's'}</dd>
            </div>
            {sections.length > 0 && (
              <div>
                <dt>Sections</dt>
                <dd>{sections.join(', ')}</dd>
              </div>
            )}
            <div>
              <dt>Standards</dt>
              <dd><Link href="/about">How PressVane reports and corrects</Link></dd>
            </div>
          </dl>
        </header>

        <h2 className="section-title" style={{ marginTop: 36 }}>
          Latest by {AUTHOR_NAME}
        </h2>

        {articles.length === 0 ? (
          <section className="empty">
            <h1>Nothing published yet</h1>
            <p>Articles appear here as they go live.</p>
          </section>
        ) : (
          <div className="grid" style={{ marginTop: 20 }}>
            {articles.map((a: Article) => (
              <ArticleCard key={a.slug} article={a} />
            ))}
          </div>
        )}
      </main>

      <Footer />
    </>
  );
}
