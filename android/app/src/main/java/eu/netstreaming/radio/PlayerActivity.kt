package eu.netstreaming.radio

import android.content.ComponentName
import android.content.Context
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ImageButton
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
    private lateinit var live: TextView
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
        play = findViewById(R.id.play)
        volume = findViewById(R.id.volume)
        sleepInfo = findViewById(R.id.sleep_info)
        title.isSelected = true

        findViewById<ImageButton>(R.id.close).setOnClickListener { finish() }
        findViewById<ImageButton>(R.id.sleep).setOnClickListener { sleepDialog() }
        findViewById<ImageButton>(R.id.prev).setOnClickListener { skip(-1) }
        findViewById<ImageButton>(R.id.next).setOnClickListener { skip(1) }
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
        live.visibility = if (st?.live == true) android.view.View.VISIBLE else android.view.View.GONE
        status.text = when {
            c.playbackState == Player.STATE_BUFFERING -> getString(R.string.connecting)
            c.isPlaying -> st?.genre?.takeIf { it.isNotBlank() } ?: "În direct"
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

    private fun updateSleep() {
        val until = Stations.prefs(this).getLong("sleep_until", 0)
        val left = (until - System.currentTimeMillis()) / 1000
        sleepInfo.text = if (left > 0) "Se oprește în ${left / 60}:${"%02d".format(left % 60)}" else ""
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
