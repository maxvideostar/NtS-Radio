package eu.netstreaming.radio

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.ConcurrentHashMap

/** caută coperta piesei (iTunes Search API); rulează pe thread de fundal */
object Covers {
    private val cache = ConcurrentHashMap<String, String>()

    fun find(title: String): String? {
        val q = title.replace(Regex("\\(.*?\\)|\\[.*?\\]"), "").trim()
        if (q.length < 3 || !q.contains(" - ")) return null
        cache[q]?.let { return it.ifEmpty { null } }
        val url = try {
            val c = URL("https://itunes.apple.com/search?media=music&entity=song&limit=1&term=" +
                URLEncoder.encode(q, "UTF-8")).openConnection() as HttpURLConnection
            c.connectTimeout = 6000; c.readTimeout = 8000
            val body = c.inputStream.bufferedReader().use { it.readText() }
            c.disconnect()
            val res = JSONObject(body).optJSONArray("results")
            if (res != null && res.length() > 0)
                res.getJSONObject(0).optString("artworkUrl100").replace("100x100", "600x600").ifBlank { null }
            else null
        } catch (e: Exception) { null }
        cache[q] = url ?: ""
        return url
    }
}
