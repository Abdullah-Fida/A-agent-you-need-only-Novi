import { Metadata } from 'next';
import Link from 'next/link';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticles, getCategories, Article } from '@/lib/supabase';
import { SITE_URL, SITE_NAME } from '@/lib/site';

interface CategoryPageProps {
  params: Promise<{ category: string }>;
}

export const revalidate = 60;

/** Pre-render a page for every category that has articles. */
export async function generateStaticParams() {
  const categories = await getCategories();
  return categories.map((category) => ({ category: encodeURIComponent(category) }));
}

export async function generateMetadata({ params }: CategoryPageProps): Promise<Metadata> {
  const { category } = await params;
  const name = decodeURIComponent(category);
  const title = `${name} News`;
  const description = `The latest ${name.toLowerCase()} coverage and analysis from ${SITE_NAME}.`;

  return {
    title,
    description,
    alternates: { canonical: `${SITE_URL}/category/${category}` },
    openGraph: {
      type: 'website',
      title,
      description,
      url: `${SITE_URL}/category/${category}`,
      siteName: SITE_NAME,
    },
  };
}

export default async function CategoryPage({ params }: CategoryPageProps) {
  const { category } = await params;
  const name = decodeURIComponent(category);
  const articles = await getArticles(50, name);
  const categories = await getCategories();

  const listSchema = {
    '@context': 'https://schema.org',
    '@type': 'CollectionPage',
    name: `${name} News`,
    url: `${SITE_URL}/category/${category}`,
    hasPart: articles.slice(0, 20).map((a) => ({
      '@type': 'NewsArticle',
      headline: a.title,
      url: `${SITE_URL}/${a.slug}`,
      datePublished: a.published_at,
    })),
  };

  return (
    <>
      <script type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(listSchema) }} />
      <Navbar />
      <main className="main-container">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <Link href="/">Home</Link>
          <span aria-hidden="true">/</span>
          <span>{name}</span>
        </nav>

        <h1 className="section-title" style={{ marginTop: 12 }}>{name}</h1>

        {categories.length > 0 && (
          <nav className="cat-strip" aria-label="Categories">
            {categories.map((c) => (
              <Link key={c} href={`/category/${encodeURIComponent(c)}`}>{c}</Link>
            ))}
          </nav>
        )}

        {articles.length === 0 ? (
          <section className="empty">
            <h1>Nothing in {name} yet</h1>
            <p>New stories appear here automatically as they are published.</p>
          </section>
        ) : (
          <div className="grid" style={{ marginTop: 28 }}>
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
