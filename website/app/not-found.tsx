import Link from 'next/link';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import ArticleCard from '@/components/ArticleCard';
import { getArticles, Article } from '@/lib/supabase';

export const metadata = {
  title: 'Page not found',
  robots: { index: false, follow: true },
};

/**
 * A 404 that offers current stories instead of a dead end. Without this
 * Next.js serves its bare default page, which loses the reader and wastes
 * the crawl.
 */
export default async function NotFound() {
  const latest = await getArticles(3);

  return (
    <>
      <Navbar />
      <main className="main-container">
        <section className="empty">
          <h1>That page isn&apos;t here</h1>
          <p>
            The story may have moved, or the link may be incomplete.
            Try the <Link href="/">front page</Link> instead.
          </p>
        </section>

        {latest.length > 0 && (
          <section style={{ maxWidth: 900, margin: '0 auto 70px' }}>
            <h2 className="section-title">Latest stories</h2>
            <div className="grid">
              {latest.map((a: Article) => (
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
