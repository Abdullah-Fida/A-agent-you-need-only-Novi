import type { Metadata } from 'next';
import Link from 'next/link';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'https://novinews.pk';
const SITE_NAME = process.env.NEXT_PUBLIC_SITE_NAME || 'Novi News';
const TELEGRAM = process.env.NEXT_PUBLIC_TELEGRAM_URL || 'https://t.me/Novi_Network';

export const metadata: Metadata = {
  title: 'About and editorial standards',
  description:
    `How ${SITE_NAME} is produced, how we use automation, how we handle sources, ` +
    `and how to reach us with a correction.`,
  alternates: { canonical: `${SITE_URL}/about` },
};

/**
 * Search engines weigh expertise, experience, authoritativeness and trust
 * heavily for news — and an "About" page stating who publishes, how, and
 * how to request a correction is one of the clearest signals of it.
 * Being explicit about the use of automation is also simply honest.
 */
export default function AboutPage() {
  const schema = {
    '@context': 'https://schema.org',
    '@type': 'AboutPage',
    name: `About ${SITE_NAME}`,
    url: `${SITE_URL}/about`,
    publisher: { '@id': `${SITE_URL}/#organization` },
  };

  return (
    <>
      <script type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(schema) }} />
      <Navbar />
      <main className="main-container">
        <article className="article">
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link href="/">Home</Link>
            <span aria-hidden="true">/</span>
            <span>About</span>
          </nav>

          <header className="article-head">
            <span className="tag">About</span>
            <h1>Editorial standards</h1>
            <p className="standfirst">
              What {SITE_NAME} covers, how each story is produced, and what to do
              if we get something wrong.
            </p>
          </header>

          <div className="prose">
            <h2>What we cover</h2>
            <p>
              {SITE_NAME} publishes continuous coverage of world affairs, crypto and
              Web3, technology, business and South Asia. We focus on explaining why a
              development matters rather than being first to report that it happened.
            </p>

            <h2>How stories are produced</h2>
            <p>
              Our newsroom is assisted by automated research and drafting. Stories begin
              as a cluster of reports from established outlets, which are cross-referenced
              before anything is written. Each article is then drafted from that
              material, and the original reporting is credited at the foot of the page
              with a link to the source.
            </p>
            <p>
              We are explicit about this because readers deserve to know how what they
              are reading was made. Automation decides <em>what</em> to cover and produces
              the first draft; the editorial standards below govern what is allowed to
              reach the page.
            </p>

            <h2>Sourcing</h2>
            <ul>
              <li>Every story is built from at least one identified publication, credited and linked.</li>
              <li>Where reports conflict, we say so rather than choosing the more dramatic version.</li>
              <li>We do not invent statistics, quotations or named sources.</li>
              <li>Where a detail is unknown, we write about the broader implications instead of speculating.</li>
            </ul>

            <h2>Corrections</h2>
            <p>
              We correct errors rather than quietly deleting them. If you find something
              inaccurate, message us on{' '}
              <a href={TELEGRAM} target="_blank" rel="noopener noreferrer">Telegram</a>{' '}
              with the article link and what is wrong. Substantive corrections are noted
              on the article itself.
            </p>

            <h2>What we are not</h2>
            <p>
              We do not publish financial advice. Coverage of markets, tokens or
              individual assets is reporting, not a recommendation to buy or sell
              anything. Do your own research and consider your own circumstances.
            </p>

            <h2>Contact</h2>
            <p>
              The fastest way to reach us is{' '}
              <a href={TELEGRAM} target="_blank" rel="noopener noreferrer">our Telegram channel</a>.
              You can also follow every story through our{' '}
              <a href="/feed.xml">RSS feed</a>.
            </p>
          </div>
        </article>
      </main>
      <Footer />
    </>
  );
}
