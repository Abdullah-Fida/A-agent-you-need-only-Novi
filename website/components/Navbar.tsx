import Link from 'next/link';

export default function Navbar() {
  return (
    <header className="header-glass">
      <div className="main-container">
        <nav className="nav-content">
          <Link href="/" className="brand-logo">
            <span className="brand-dot"></span>
            <span>DAILY PULSE <span className="gradient-text">PK</span></span>
          </Link>
          
          <ul className="nav-links">
            <li><Link href="/" className="nav-link">Home</Link></li>
            <li><Link href="/category/economy" className="nav-link">Economy</Link></li>
            <li><Link href="/category/tech" className="nav-link">Tech & AI</Link></li>
            <li><Link href="/category/pakistan" className="nav-link">Pakistan</Link></li>
          </ul>

          <a 
            href="https://t.me/DailyPulsePK" 
            target="_blank" 
            rel="noopener noreferrer" 
            className="btn-telegram"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69.01-.03.01-.14-.07-.2-.08-.06-.19-.04-.27-.02-.12.02-1.96 1.25-5.54 3.69-.52.36-1 .54-1.43.53-.47-.01-1.37-.26-2.04-.48-.82-.27-1.47-.42-1.42-.88.03-.24.37-.49 1.02-.75 3.99-1.74 6.66-2.89 8.01-3.46 3.82-1.6 4.61-1.88 5.13-1.89.11 0 .37.03.54.17.14.12.18.28.2.45-.02.07-.02.14-.04.22z"/>
            </svg>
            Join Telegram
          </a>
        </nav>
      </div>
    </header>
  );
}
