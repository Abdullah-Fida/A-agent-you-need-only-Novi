import Link from 'next/link';
import { SITE_NAME, TELEGRAM_URL as TELEGRAM } from '@/lib/site';

export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="main-container">
        <div className="footer-grid">
          <div className="footer-brand">
            <span className="wordmark">
              <span className="mark" aria-hidden="true" />
              <span>{SITE_NAME}</span>
            </span>
            <p>
              Independent coverage of world affairs, crypto and Web3, technology,
              business and South Asia — published continuously, with the context
              behind each story.
            </p>
          </div>

          <div className="footer-col">
            <h4>Sections</h4>
            <ul>
              <li><Link href="/category/World">World</Link></li>
              <li><Link href="/category/Crypto">Crypto</Link></li>
              <li><Link href="/category/Tech">Technology</Link></li>
              <li><Link href="/category/Business">Business</Link></li>
              <li><Link href="/category/Pakistan">Pakistan</Link></li>
            </ul>
          </div>

          <div className="footer-col">
            <h4>Follow</h4>
            <ul>
              <li><a href={TELEGRAM} target="_blank" rel="noopener noreferrer">Telegram</a></li>
              <li><Link href="/about">About &amp; standards</Link></li>
              <li><Link href="/privacy">Privacy policy</Link></li>
              <li><a href="/feed.xml">RSS feed</a></li>
              <li><a href="/sitemap.xml">Sitemap</a></li>
            </ul>
          </div>
        </div>

        <div className="footer-base">
          <span>© {new Date().getFullYear()} {SITE_NAME}</span>
          {/* How the newsroom works is explained properly on /about, where a
              reader looking for it will go. Repeating it on every page read
              as a machine apologising for itself. */}
          <Link href="/about">Editorial standards</Link>
        </div>
      </div>
    </footer>
  );
}
