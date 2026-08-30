import type { Metadata } from 'next';
import Link from 'next/link';
import Navbar from '@/components/Navbar';
import Footer from '@/components/Footer';
import { SITE_URL, SITE_NAME } from '@/lib/site';

export const metadata: Metadata = {
  title: 'Privacy policy',
  description:
    `What ${SITE_NAME} collects, what it does not, and how the site handles ` +
    `data. No advertising trackers, no analytics, no accounts.`,
  alternates: { canonical: `${SITE_URL}/privacy` },
};

/**
 * Written against what the site actually does, not from a template.
 *
 * There is no analytics package, no advertising tag, no cookie of any kind
 * and no account system, so the honest version of this page is short. Saying
 * more than is true would be its own kind of lie, and a page claiming to set
 * cookies that do not exist helps nobody.
 *
 * Required regardless of size: readers in the EU and UK have a right to this
 * information, and any future ad or affiliate programme will ask for it.
 */
export default function PrivacyPage() {
  const schema = {
    '@context': 'https://schema.org',
    '@type': 'WebPage',
    name: `Privacy policy — ${SITE_NAME}`,
    url: `${SITE_URL}/privacy`,
    publisher: { '@id': `${SITE_URL}/#organization` },
  };

  const updated = 'August 2026';

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
            <span>Privacy</span>
          </nav>

          <header className="article-head">
            <span className="tag">Legal</span>
            <h1>Privacy policy</h1>
            <p className="standfirst">
              {SITE_NAME} does not track you. This page explains exactly what
              that means and what little data exists.
            </p>
            <div className="byline">
              <span>Last updated {updated}</span>
            </div>
          </header>

          <div className="article-body">
            <h2>The short version</h2>
            <p>
              We do not run analytics, advertising tags or social pixels. We set
              no tracking cookies. There are no accounts, no newsletter and no
              comments, so there is nothing for you to sign up to and no personal
              details for us to hold.
            </p>

            <h2>What is stored on your device</h2>
            <p>
              One item, and only if you use the light/dark switch: your theme
              choice is saved in your browser&apos;s local storage so the site
              does not flash the wrong colour scheme on your next visit. It never
              leaves your device and we cannot read it. Clearing your browser
              data removes it.
            </p>

            <h2>What our hosts see</h2>
            <p>
              Like every website, the servers that deliver these pages record
              ordinary request logs — IP address, time, page requested, browser
              type. This is standard infrastructure logging, kept briefly and
              used to keep the site running and to stop abuse.
            </p>
            <p>
              The site is served by Vercel and its articles and images are stored
              with Supabase. Both process this data as our providers, under their
              own privacy terms.
            </p>

            <h2>Links to other sites</h2>
            <p>
              Every article credits the outlet that reported the story first and
              links to it. Once you follow a link you are on their site, under
              their privacy policy, not ours.
            </p>

            <h2>Children</h2>
            <p>
              This is a general news site and is not directed at children. We do
              not knowingly collect information from anyone.
            </p>

            <h2>Your rights</h2>
            <p>
              Readers in the UK, EU and similar jurisdictions have the right to
              ask what data is held about them, to have it corrected, and to have
              it deleted. In our case the answer is that we hold none — but if
              you would like that confirmed in writing, ask and we will confirm
              it.
            </p>

            <h2>Changes</h2>
            <p>
              If this ever changes — if we add analytics, advertising or a
              newsletter — this page will be updated before the change goes live,
              and the date at the top will say so.
            </p>

            <h2>Contact</h2>
            <p>
              Questions about this policy, or about anything on the site, can go
              through the details on our{' '}
              <Link href="/about">editorial standards page</Link>.
            </p>
          </div>
        </article>
      </main>
      <Footer />
    </>
  );
}
