package eu.netstreaming.radio

import android.content.ComponentName
import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.graphics.RenderEffect
import android.graphics.Shader
import android.os.Build
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.View
import android.view.animation.AlphaAnimation
import android.view.animation.Animation
import android.widget.ImageButton
import android.widget.LinearLayout
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import android.widget.ImageView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import java.util.concurrent.Executors

@androidx.media3.common.util.UnstableApi
class PlayerActivity : AppCompatActivity() {

    private var controllerFuture: ListenableFuture<MediaController>? = null
    private var controller: MediaController? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var audio: AudioManager
    private lateinit var art: ImageView
    private lateinit var bars: BarsView
    private lateinit var station: TextView
    private lateinit var title: TextView
    private lateinit var status: TextView
    private lateinit var live: View
    private lateinit var logo: ImageView
    private lateinit var genre: TextView
    private lateinit var bgart: ImageView
    private lateinit var hist: LinearLayout
    private lateinit var histbox: View
    private lateinit var links: LinearLayout
    private var histSlug: String? = null
    private var histAt = 0L
    private var shownBg: String? = null
    private lateinit var play: ImageButton
    private lateinit var volume: SeekBar
    private lateinit var sleepInfo: TextView
    private var shownTitle: String? = null
    private var shownArt: String? = null
    private var vizUrl: String? = null        // sigla implicită pentru care avem egalizator
    private var vizLogo: String? = null
    private var vizOn = false

    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) = update()
    }

    private val ticker = object : Runnable {
        override fun run() {
            updateSleep()
            volume.progress = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
            main.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_player)
        audio = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        art = findViewById(R.id.art)
        bars = findViewById(R.id.bars)
        station = findViewById(R.id.station)
        title = findViewById(R.id.title)
        status = findViewById(R.id.status)
        live = findViewById(R.id.live)
        logo = findViewById(R.id.logo)
        genre = findViewById(R.id.genre)
        bgart = findViewById(R.id.bgart)
        hist = findViewById(R.id.hist)
        histbox = findViewById(R.id.histbox)
        links = findViewById(R.id.links)
        if (Build.VERSION.SDK_INT >= 31)
            bgart.setRenderEffect(RenderEffect.createBlurEffect(120f, 120f, Shader.TileMode.CLAMP))
        findViewById<View>(R.id.liveDot).startAnimation(AlphaAnimation(1f, 0.25f).apply {
            duration = 600; repeatMode = Animation.REVERSE; repeatCount = Animation.INFINITE
        })
        // schimbarea postului: glisare stânga / dreapta pe copertă
        val swipe = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true
            override fun onFling(e1: MotionEvent?, e2: MotionEvent, vx: Float, vy: Float): Boolean {
                val dx = e2.x - (e1?.x ?: e2.x)
                if (kotlin.math.abs(dx) > 120 && kotlin.math.abs(vx) > kotlin.math.abs(vy)) { skip(if (dx < 0) 1 else -1); return true }
                return false
            }
        })
        findViewById<View>(R.id.coverCard).setOnTouchListener { _, ev -> swipe.onTouchEvent(ev) }
        play = findViewById(R.id.play)
        volume = findViewById(R.id.volume)
        sleepInfo = findViewById(R.id.sleep_info)

        findViewById<ImageButton>(R.id.close).setOnClickListener { finish() }
        findViewById<ImageButton>(R.id.sleep).setOnClickListener { sleepDialog() }
        play.setOnClickListener {
            val c = controller ?: return@setOnClickListener
            if (c.isPlaying || c.playbackState == Player.STATE_BUFFERING) c.stop()
            else if (c.mediaItemCount > 0) { c.prepare(); c.play() }
        }

        volume.max = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        volume.progress = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
        volume.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar?, p: Int, fromUser: Boolean) {
                if (fromUser) audio.setStreamVolume(AudioManager.STREAM_MUSIC, p, 0)
            }
            override fun onStartTrackingTouch(s: SeekBar?) {}
            override fun onStopTrackingTouch(s: SeekBar?) {}
        })
    }

    override fun onStart() {
        super.onStart()
        val token = SessionToken(this, ComponentName(this, PlaybackService::class.java))
        val f = MediaController.Builder(this, token).buildAsync()
        controllerFuture = f
        f.addListener({
            controller = runCatching { f.get() }.getOrNull()
            controller?.addListener(listener)
            update()
        }, ContextCompat.getMainExecutor(this))
        main.post(ticker)
    }

    override fun onStop() {
        main.removeCallbacks(ticker)
        controller?.removeListener(listener)
        controllerFuture?.let { MediaController.releaseFuture(it) }
        controller = null
        super.onStop()
    }

    override fun onDestroy() {
        io.shutdown()
        super.onDestroy()
    }

    private fun skip(dir: Int) {
        val c = controller ?: return
        val list = Stations.list
        if (list.isEmpty()) return
        val i = list.indexOfFirst { it.slug == c.currentMediaItem?.mediaId }
        val s = list[((if (i < 0) 0 else i + dir) % list.size + list.size) % list.size]
        c.setMediaItem(Stations.toMediaItem(s))
        c.prepare()
        c.play()
        Stations.prefs(this).edit().putString("last", s.slug).apply()
    }

    private fun update() {
        val c = controller ?: return
        val item = c.currentMediaItem ?: run { finish(); return }
        val st = Stations.find(item.mediaId)
        val stationName = st?.name ?: item.mediaMetadata.station?.toString() ?: ""
        val t = c.mediaMetadata.title?.toString()?.takeIf { it.isNotBlank() } ?: stationName
        station.text = stationName
        title.text = t
        live.visibility = if (st?.live == true) View.VISIBLE else View.GONE
        if (st != null && st.genre.isNotBlank()) { genre.text = st.genre; genre.visibility = View.VISIBLE } else genre.visibility = View.GONE
        if (logo.tag != st?.logo) ImageLoader.load(st?.logo, logo, R.mipmap.ic_launcher)
        if (st != null && (st.slug != histSlug || System.currentTimeMillis() - histAt > 20000)) loadHistory(st)
        status.text = when {
            c.playbackState == Player.STATE_BUFFERING -> getString(R.string.connecting)
            c.isPlaying -> if (st?.live == true) "În direct din studio" else "Muzică non-stop"
            else -> "Oprit"
        }
        val active = c.isPlaying || c.playbackState == Player.STATE_BUFFERING
        applyViz(c.isPlaying)
        play.setImageResource(if (active) R.drawable.ic_stop else R.drawable.ic_play)

        if (t != shownTitle) {
            shownTitle = t
            val logo = st?.logo ?: item.mediaMetadata.artworkUri?.toString()
            if (shownArt == null) { showArt(logo, logo, st); shownArt = logo }
            io.execute {
                val cover = if (t != stationName) Covers.find(t) else null
                main.post {
                    if (shownTitle == t) {
                        val u = cover ?: logo
                        if (u != shownArt) { shownArt = u; showArt(u, logo, st) }
                    }
                }
            }
        }
    }

    /** sigla implicită netstreaming.eu: imaginea originală pe stop, iar în redare fundal + bare live identice */
    private fun showArt(url: String?, logo: String?, st: Station?) {
        if (url != shownBg) { shownBg = url; ImageLoader.load(url, bgart, android.R.color.transparent, keep = true) }
        val viz = st != null && st.logoDefault && st.vizBg.isNotBlank() && url == logo
        vizUrl = if (viz) st!!.vizBg else null
        ImageLoader.prefetch(vizUrl)
        vizLogo = url
        vizOn = false
        bars.running = false
        bars.visibility = android.view.View.GONE
        ImageLoader.load(url, art, R.mipmap.ic_launcher)
        applyViz(controller?.isPlaying == true)
    }

    private fun applyViz(playing: Boolean) {
        val on = playing && vizUrl != null
        if (on == vizOn) return
        vizOn = on
        if (on) {
            ImageLoader.load(vizUrl, art, R.mipmap.ic_launcher, keep = true) {
                if (vizOn) {                      // barele apar doar după ce fundalul fără bare e afișat
                    bars.visibility = android.view.View.VISIBLE
                    bars.running = true
                }
            }
        } else {
            bars.running = false
            bars.visibility = android.view.View.GONE
            ImageLoader.load(vizLogo, art, R.mipmap.ic_launcher, keep = true)
        }
    }

    /** „Ultimele piese” + formatele stream-ului, din /api/public/<post> (ca în player-ul web) */
    private fun loadHistory(st: Station) {
        histSlug = st.slug
        histAt = System.currentTimeMillis()
        val url = Stations.server(this) + "/api/public/" + st.slug
        io.execute {
            val j = try {
                val c = URL(url).openConnection() as HttpURLConnection
                c.connectTimeout = 8000; c.readTimeout = 10000
                c.setRequestProperty("User-Agent", Stations.UA)
                JSONObject(c.inputStream.bufferedReader().use { it.readText() }).also { c.disconnect() }
            } catch (e: Exception) { null } ?: return@execute
            val h = j.optJSONArray("history")
            val streams = j.optJSONArray("streams")
            val fmt = SimpleDateFormat("HH:mm", Locale.getDefault())
            val rows = ArrayList<Pair<String, String>>()
            if (h != null) for (i in 0 until h.length()) {
                val o = h.getJSONObject(i)
                rows.add(fmt.format(Date(o.optLong("ts") * 1000)) to o.optString("title"))
            }
            val chips = ArrayList<String>()
            if (streams != null) for (i in 0 until streams.length()) {
                val o = streams.getJSONObject(i)
                chips.add("${o.optString("codec").uppercase()} ${o.optInt("bitrate")}k")
            }
            main.post { if (histSlug == st.slug) renderHistory(rows, chips) }
        }
    }

    private fun renderHistory(rows: List<Pair<String, String>>, chips: List<String>) {
        val d = resources.displayMetrics.density
        hist.removeAllViews()
        rows.forEachIndexed { i, (time, t) ->
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, (7 * d).toInt(), 0, (7 * d).toInt())
            }
            row.addView(TextView(this).apply {
                text = time; textSize = 14f; setTextColor(0xFF9A9CA3.toInt())
                fontFeatureSettings = "tnum"
            })
            row.addView(TextView(this).apply {
                text = t; textSize = 14f; setTextColor(0xFFF2F2F0.toInt())
                setPadding((12 * d).toInt(), 0, 0, 0)
            })
            hist.addView(row)
            if (i < rows.size - 1) hist.addView(View(this).apply {
                setBackgroundColor(0x0FFFFFFF)
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, maxOf(1, d.toInt()))
            })
        }
        histbox.visibility = if (rows.isEmpty()) View.GONE else View.VISIBLE
        links.removeAllViews()
        chips.forEachIndexed { i, c ->
            links.addView(TextView(this).apply {
                text = c; textSize = 12f; setTextColor(0xFF9A9CA3.toInt())
                setBackgroundResource(R.drawable.bg_web_chip)
                setPadding((12 * d).toInt(), (6 * d).toInt(), (12 * d).toInt(), (6 * d).toInt())
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
                    .apply { if (i > 0) marginStart = (10 * d).toInt() }
            })
        }
    }

    private fun updateSleep() {
        val until = Stations.prefs(this).getLong("sleep_until", 0)
        val left = (until - System.currentTimeMillis()) / 1000
        sleepInfo.text = if (left > 0) "Se oprește în ${left / 60}:${"%02d".format(left % 60)}" else ""
        sleepInfo.visibility = if (left > 0) View.VISIBLE else View.GONE
    }

    private fun sleepDialog() {
        val opts = arrayOf("Dezactivat", "15 minute", "30 minute", "45 minute", "60 minute", "90 minute")
        val mins = intArrayOf(0, 15, 30, 45, 60, 90)
        AlertDialog.Builder(this).setTitle(R.string.sleep).setItems(opts) { _, i ->
            setSleep(this, controller, mins[i])
            updateSleep()
        }.show()
    }

    companion object {
        fun setSleep(ctx: Context, c: MediaController?, minutes: Int) {
            val args = Bundle().apply { putInt(PlaybackService.ARG_MINUTES, minutes) }
            c?.sendCustomCommand(SessionCommand(PlaybackService.CMD_SLEEP, Bundle.EMPTY), args)
            Stations.prefs(ctx).edit()
                .putLong("sleep_until", if (minutes > 0) System.currentTimeMillis() + minutes * 60_000L else 0)
                .apply()
            Toast.makeText(ctx, if (minutes == 0) "Oprire automată dezactivată" else "Se oprește în $minutes minute",
                Toast.LENGTH_SHORT).show()
        }
    }
}
