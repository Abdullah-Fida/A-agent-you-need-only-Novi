import { MetadataRoute } from 'next';

const SITE_NAME = process.env.NEXT_PUBLIC_SITE_NAME || 'Novi News';

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: `${SITE_NAME} — World, Crypto, Tech & Business News`,
    short_name: SITE_NAME,
    description:
      'Independent coverage of world affairs, crypto and Web3, technology, ' +
      'business and South Asia.',
    start_url: '/',
    display: 'standalone',
    background_color: '#FBFAF7',
    theme_color: '#0F6B4F',
    categories: ['news', 'magazines'],
    icons: [
      { src: '/icon-192.png', sizes: '192x192', type: 'image/png' },
      { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
    ],
  };
}
