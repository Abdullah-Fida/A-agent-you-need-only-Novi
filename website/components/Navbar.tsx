import Link from 'next/link';
import { getCategories } from '@/lib/supabase';
import { SITE_NAME } from '@/lib/site';
import ThemeToggle from './ThemeToggle';

/** Fallback nav until the database has articles to derive categories from. */
const DEFAULT_SECTIONS = ['World', 'Crypto', 'Tech', 'Business', 'Pakistan'];

/**
 * The masthead.
 *
 * The wordmark sits top-LEFT. It was centred, which looked handsome and broke
 * the one navigation convention every reader relies on -- logo top-left goes
 * home -- so people could not find their way back from a section page even
 * though the link was there the whole time. Convention beats symmetry.
 */
export default async function Navbar() {
  const found = await getCategories();
  const sections = (found.length ? found : DEFAULT_SECTIONS).slice(0, 6);

  const now = new Date();
  const today = now.toLocaleDateString('en-US', {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  });

  return (
    <header className="masthead">
      <div className="main-container">
        <div className="masthead-top">
          <Link href="/" className="wordmark" aria-label={`${SITE_NAME} home`}>
            {SITE_NAME}
          </Link>

          <div className="masthead-meta">
            <time dateTime={now.toISOString().slice(0, 10)}>{today}</time>
            <ThemeToggle />
          </div>
        </div>
      </div>

      <div className="masthead-rule">
        <div className="main-container">
          <nav aria-label="Sections" className="sections">
            <ul>
              <li>
                <Link href="/" className="sections-home">Home</Link>
              </li>
              {sections.map((s) => (
                <li key={s}>
                  <Link href={`/category/${encodeURIComponent(s)}`}>{s}</Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </div>
    </header>
  );
}
