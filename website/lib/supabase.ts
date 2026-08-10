import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || '';
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';

if (!supabaseUrl || !supabaseAnonKey) {
  // Surfaced at build time so a missing env var is obvious rather than
  // silently rendering an empty site.
  console.warn(
    '[novi] NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY are not set. ' +
    'No articles will load.'
  );
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey);

/** Mirrors the `articles` table in database/schema.sql. */
export interface Article {
  id: string;
  title: string;
  slug: string;
  content: string;
  summary: string;
  main_image_url: string;
  category: string;
  seo_keywords: string[];
  meta_title?: string;
  meta_description?: string;
  reading_minutes?: number;
  word_count?: number;
  source_url?: string;
  source_name?: string;
  author: string;
  status?: string;
  views?: number;
  published_at: string;
  created_at?: string;
}

/** Latest published articles, newest first. */
export async function getArticles(limit = 24, category?: string): Promise<Article[]> {
  try {
    let query = supabase
      .from('articles')
      .select('*')
      .eq('status', 'published')
      .order('published_at', { ascending: false })
      .limit(limit);

    if (category) query = query.eq('category', category);

    const { data, error } = await query;
    if (error) {
      console.error('[novi] Failed to load articles:', error.message);
      return [];
    }
    return (data as Article[]) || [];
  } catch (err) {
    console.error('[novi] Unexpected error loading articles:', err);
    return [];
  }
}

/** Distinct categories that actually have published articles. */
export async function getCategories(): Promise<string[]> {
  try {
    const { data } = await supabase
      .from('articles')
      .select('category')
      .eq('status', 'published');
    const set = new Set((data || []).map((r: { category: string }) => r.category).filter(Boolean));
    return Array.from(set).sort();
  } catch {
    return [];
  }
}
