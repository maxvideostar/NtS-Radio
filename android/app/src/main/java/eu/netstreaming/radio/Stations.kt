package eu.netstreaming.radio

import android.content.Context
import android.net.Uri
import android.os.Bundle
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

data class Station(
    val slug: String,
    val name: String,
    val genre: String,
    val title: String,
    val live: Boolean,
    val onAir: Boolean,
    val listeners: Int,
    val logo: String,
    val streamUrl: String,
    val logoDefault: Boolean = false,
    val vizBg: String = "",
)

object Stations {
    const val PREFS = "nts"
    const val UA = "NetStreamingRadio/1.0 (Linux; Android) ExoPlayer"

    @Volatile
    var list: List<Station> = emptyList()
        private set

    fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun server(ctx: Context): String =
        (prefs(ctx).getString("server", null) ?: BuildConfig.DEFAULT_SERVER).trimEnd('/')

    fun quality(ctx: Context): String = prefs(ctx).getString("quality", "high") ?: "high"

    fun find(slug: String?): Station? = list.firstOrNull { it.slug == slug }

    /** descarcă lista posturilor de pe server (rulează pe un thread de fundal) */
    fun fetch(ctx: Context): List<Station> {
        val conn = URL(server(ctx) + "/api/public").openConnection() as HttpURLConnection
        conn.connectTimeout = 10000
        conn.readTimeout = 15000
        conn.setRequestProperty("User-Agent", UA)
        try {
            if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            val arr = JSONArray(body)
            val high = quality(ctx) == "high"
            val res = ArrayList<Station>()
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                val streams = o.optJSONArray("streams") ?: continue
                if (streams.length() == 0) continue
                var best = streams.getJSONObject(0)
                for (j in 1 until streams.length()) {
                    val s = streams.getJSONObject(j)
                    val br = s.optInt("bitrate"); val bb = best.optInt("bitrate")
                    if ((high && br > bb) || (!high && br < bb) ||
                        (br == bb && s.optString("codec") == "mp3")) best = s
                }
                res.add(
                    Station(
                        slug = o.optString("slug"),
                        name = o.optString("name"),
                        genre = o.optString("genre").takeIf { it != "null" } ?: "",
                        title = o.optString("title").takeIf { it != "null" } ?: "",
                        live = o.optBoolean("live"),
                        onAir = o.optBoolean("on_air", true),
                        listeners = o.optInt("listeners"),
                        logo = o.optString("logo"),
                        streamUrl = best.optString("url"),
                        logoDefault = o.optBoolean("logo_default"),
                        vizBg = o.optString("viz_bg").takeIf { it != "null" } ?: "",
                    )
                )
            }
            list = res
            return res
        } finally {
            conn.disconnect()
        }
    }

    fun toMediaItem(s: Station): MediaItem {
        val extras = Bundle().apply { putString("slug", s.slug) }
        val md = MediaMetadata.Builder()
            .setTitle(s.name)
            .setStation(s.name)
            .setArtist(if (s.title.isNotBlank() && s.title != s.name) s.title else s.genre)
            .setDisplayTitle(s.name)
            .setSubtitle(if (s.title.isNotBlank()) s.title else s.genre)
            .setArtworkUri(if (s.logo.isNotBlank()) Uri.parse(s.logo) else null)
            .setIsPlayable(true)
            .setIsBrowsable(false)
            .setMediaType(MediaMetadata.MEDIA_TYPE_RADIO_STATION)
            .setExtras(extras)
            .build()
        return MediaItem.Builder()
            .setMediaId(s.slug)
            .setUri(s.streamUrl)
            .setMediaMetadata(md)
            .build()
    }
}
