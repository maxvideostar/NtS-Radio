package eu.netstreaming.radio

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.util.LruCache
import android.widget.ImageView
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/** încărcare simplă de imagini (logo-urile posturilor), cu cache în memorie */
object ImageLoader {
    private val cache = LruCache<String, Bitmap>(40)
    private val pool = Executors.newFixedThreadPool(3)
    private val main = Handler(Looper.getMainLooper())

    fun load(url: String?, view: ImageView, fallback: Int) {
        if (url.isNullOrBlank()) { view.setImageResource(fallback); view.tag = null; return }
        view.tag = url
        cache.get(url)?.let { view.setImageBitmap(it); return }
        view.setImageResource(fallback)
        pool.execute {
            val bmp = try {
                val c = URL(url).openConnection() as HttpURLConnection
                c.connectTimeout = 8000; c.readTimeout = 10000
                c.setRequestProperty("User-Agent", Stations.UA)
                c.inputStream.use { BitmapFactory.decodeStream(it) }.also { c.disconnect() }
            } catch (e: Exception) { null }
            if (bmp != null) {
                cache.put(url, bmp)
                main.post { if (view.tag == url) view.setImageBitmap(bmp) }
            }
        }
    }
}
