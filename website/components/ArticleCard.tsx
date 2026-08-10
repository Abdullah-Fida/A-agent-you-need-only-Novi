import Link from 'next/link';
import { Article } from '@/lib/supabase';

interface ArticleCardProps {
  article: Article;
}

export default function ArticleCard({ article }: ArticleCardProps) {
  const formattedDate = new Date(article.published_at).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric'
  });

  return (
    <Link href={`/${article.slug}`}>
      <article className="glass-panel article-card">
        <div className="card-image-wrap">
          <img 
            src={article.main_image_url || '/placeholder-news.jpg'} 
            alt={article.title} 
            className="card-image" 
            loading="lazy"
          />
          <span className="card-badge">{article.category || 'News'}</span>
        </div>
        <div className="card-body">
          <h2 className="card-title">{article.title}</h2>
          <p className="card-summary">{article.summary}</p>
          <div className="card-footer">
            <span>By {article.author || 'NOVI Agent'}</span>
            <time>{formattedDate}</time>
          </div>
        </div>
      </article>
    </Link>
  );
}
