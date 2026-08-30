import Link from 'next/link';
import { getCategories } from '@/lib/supabase';
import { SITE_NAME } from '@/lib/site';
import ThemeToggle from './ThemeToggle';

/** Fallback nav until the database has articles to derive categories from. */
const DEFAULT_SECTIONS = ['World', 'Crypto', 'Tech', 'Business', 'Pakistan'];

/**
 * The masthead.
 *
 * Laid out the way a newspaper front page is: the dateline and the utilities
 * sit either side of a centred wordmark, with the sections on their own rule
 * beneath. Putting the name in the optical centre is what makes a masthead
 * read as a publication rather than as an app header with a logo in the
 * corner.
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
          <div className="masthead-side masthead-side--left">
            <time dateTime={now.toISOString().slice(0, 10)}>{today}</time>
          </div>

          <Link href="/" className="wordmark" aria-label={`${SITE_NAME} home`}>
            {SITE_NAME}
          </Link>

          <div className="masthead-side masthead-side--right">
            <span className="live">
              <span className="live-dot" aria-hidden="true" />
              Updating continuously
            </span>
            <ThemeToggle />
          </div>
        </div>
      </div>

      <div className="masthead-rule">
        <div className="main-container">
          <nav aria-label="Sections" className="sections">
            <ul>
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
