import type { Metadata } from "next";
import { Newsreader, Public_Sans } from "next/font/google";
import "./globals.css";
import { SITE_URL, SITE_NAME } from '@/lib/site';

/*
 * Fonts are loaded through next/font, not a CSS @import.
 *
 * An @import inside globals.css is render-blocking: the browser must fetch
 * the CSS, parse it, then fetch Google's stylesheet, then the font files —
 * three serial round-trips before any text paints. next/font self-hosts the
 * files at build time, inlines the @font-face rules and preloads them, which
 * removes the round-trips entirely and eliminates layout shift.
 *
 * That matters for ranking: LCP and CLS are Core Web Vitals.
 */
// Both are variable fonts, so no `weight` is specified — the whole axis is
// available and only one file is downloaded per style.
const newsreader = Newsreader({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-display",
  style: ["normal", "italic"],
});

const publicSans = Public_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-ui",
});

const DESCRIPTION =
  "Independent coverage of world affairs, crypto and Web3, technology, business " +
  "and South Asia — published continuously, with the context behind each story.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: `${SITE_NAME} — World, Crypto, Tech & Business News`,
    template: `%s | ${SITE_NAME}`,
  },
  description: DESCRIPTION,
  applicationName: SITE_NAME,
  keywords: [
    "news", "world news", "crypto news", "bitcoin", "technology news",
    "business news", "pakistan news", "market analysis", "web3",
  ],
  authors: [{ name: SITE_NAME, url: SITE_URL }],
  creator: SITE_NAME,
  publisher: SITE_NAME,
  manifest: "/manifest.webmanifest",
  alternates: {
    canonical: "/",
    types: { "application/rss+xml": `${SITE_URL}/feed.xml` },
  },
  icons: {
    icon: [
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/apple-icon.png", sizes: "180x180", type: "image/png" }],
  },
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    title: `${SITE_NAME} — World, Crypto, Tech & Business News`,
    description: DESCRIPTION,
    url: SITE_URL,
    locale: "en_US",
    images: [{
      url: "/og-default.png",
      width: 1200,
      height: 630,
      alt: SITE_NAME,
    }],
  },
  twitter: {
    card: "summary_large_image",
    title: `${SITE_NAME} — World, Crypto, Tech & Business News`,
    description: DESCRIPTION,
    images: ["/og-default.png"],
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  category: "news",
};

export const viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FBFAF7" },
    { media: "(prefers-color-scheme: dark)", color: "#101114" },
  ],
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // Publisher identity. Google needs this to attribute articles to an
  // organisation and to enable rich results in Search and News.
  const orgSchema = {
    "@context": "https://schema.org",
    "@type": "NewsMediaOrganization",
    "@id": `${SITE_URL}/#organization`,
    name: SITE_NAME,
    url: SITE_URL,
    description: DESCRIPTION,
    logo: {
      "@type": "ImageObject",
      url: `${SITE_URL}/logo.png`,
      width: 512,
      height: 512,
    },
  };

  const siteSchema = {
    "@context": "https://schema.org",
    "@type": "WebSite",
    "@id": `${SITE_URL}/#website`,
    name: SITE_NAME,
    url: SITE_URL,
    publisher: { "@id": `${SITE_URL}/#organization` },
    inLanguage: "en",
  };

  return (
    <html
      lang="en"
      className={`${newsreader.variable} ${publicSans.variable}`}
      suppressHydrationWarning
    >
      <head>
        {/* Warm up the Supabase origin — every page fetches from it */}
        {process.env.NEXT_PUBLIC_SUPABASE_URL && (
          <link rel="preconnect" href={process.env.NEXT_PUBLIC_SUPABASE_URL} />
        )}
        {/*
          Applies a saved theme choice before first paint. Without this the
          page renders in the system theme and then snaps to the chosen one,
          which is a visible flash on every navigation.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{var t=localStorage.getItem('pressvane-theme');" +
              "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}catch(e){}",
          }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(orgSchema) }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(siteSchema) }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
