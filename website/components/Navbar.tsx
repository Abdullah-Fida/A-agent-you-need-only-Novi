import Link from 'next/link';
import { getCategories } from '@/lib/supabase';
import { SITE_NAME, TELEGRAM_URL as TELEGRAM } from '@/lib/site';
import ThemeToggle from './ThemeToggle';

/** Fallback nav until the database has articles to derive categories from. */
const DEFAULT_SECTIONS = ['World', 'Crypto', 'Tech', 'Business', 'Pakistan'];

export default async function Navbar() {
  const found = await getCategories();
  const sections = (found.length ? found : DEFAULT_SECTIONS).slice(0, 6);

  const today = new Date().toLocaleDateString('en-US', {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  });

  return (
    <header className="masthead">
      <div className="main-container">
        <div className="masthead-inner">
          <Link href="/" className="wordmark" aria-label={`${SITE_NAME} home`}>
            <span className="mark" aria-hidden="true" />
            <span>{SITE_NAME}</span>
          </Link>

          <nav aria-label="Sections">
            <ul>
              {sections.map((s) => (
                <li key={s}>
                  <Link href={`/category/${encodeURIComponent(s)}`}>{s}</Link>
                </li>
              ))}
            </ul>
          </nav>

          <div className="masthead-actions">
            <ThemeToggle />
            <a className="btn-follow" href={TELEGRAM} target="_blank" rel="noopener noreferrer">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69.01-.03.01-.14-.07-.2-.08-.06-.19-.04-.27-.02-.12.02-1.96 1.25-5.54 3.69-.52.36-1 .54-1.43.53-.47-.01-1.37-.26-2.04-.48-.82-.27-1.47-.42-1.42-.88.03-.24.37-.49 1.02-.75 3.99-1.74 6.66-2.89 8.01-3.46 3.82-1.6 4.61-1.88 5.13-1.89.11 0 .37.03.54.17.14.12.18.28.2.45-.02.07-.02.14-.04.22z" />
            </svg>
              Follow
            </a>
          </div>
        </div>
      </div>

      <div className="main-container">
        <div className="dateline">
          <span>{today}</span>
          <span className="live">Updating continuously</span>
        </div>
      </div>
    </header>
  );
}
