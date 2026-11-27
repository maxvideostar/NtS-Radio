package eu.netstreaming.radio

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.audio.TeeAudioProcessor
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.hypot
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.roundToInt

/**
 * Analizează sunetul exact cum îl redă player-ul (fără permisiune de microfon):
 * FFT pe 1024 eșantioane -> 17 benzi logaritmice 50 Hz – 14 kHz, ca egalizatorul din sigla netstreaming.eu.
 */
@UnstableApi
object Levels : TeeAudioProcessor.AudioBufferSink {
    const val BANDS = 17
    private const val N = 1024
    private const val LATENCY_NS = 250_000_000L       // bufferul AudioTrack: sincronizăm barele cu ce se aude

    private class Frame(val t: Long, val v: FloatArray)

    private val queue = ArrayDeque<Frame>()
    private var sr = 44100
    private var ch = 2
    private var enc = C.ENCODING_PCM_16BIT
    private val win = FloatArray(N)
    private var pos = 0
    private val re = FloatArray(N)
    private val im = FloatArray(N)
    private val hann = FloatArray(N) { (0.5 - 0.5 * cos(2 * PI * it / (N - 1))).toFloat() }
    private var edges = makeEdges(44100)

    private fun makeEdges(rate: Int): IntArray {
        val lo = ln(50.0)
        val hi = ln(14000.0)
        return IntArray(BANDS + 1) { i ->
            (exp(lo + (hi - lo) * i / BANDS) / (rate / 2.0) * (N / 2)).roundToInt().coerceIn(1, N / 2 - 1)
        }
    }

    override fun flush(sampleRateHz: Int, channelCount: Int, encoding: Int) {
        sr = sampleRateHz
        ch = channelCount.coerceAtLeast(1)
        enc = encoding
        pos = 0
        edges = makeEdges(sr)
        synchronized(queue) { queue.clear() }
    }

    override fun handleBuffer(buffer: ByteBuffer) {
        val b = buffer.duplicate().order(ByteOrder.nativeOrder())
        when (enc) {
            C.ENCODING_PCM_16BIT -> while (b.remaining() >= 2 * ch) {
                var s = 0f
                repeat(ch) { s += b.short / 32768f }
                push(s / ch)
            }
            C.ENCODING_PCM_FLOAT -> while (b.remaining() >= 4 * ch) {
                var s = 0f
                repeat(ch) { s += b.float }
                push(s / ch)
            }
            else -> return
        }
    }

    private fun push(v: Float) {
        win[pos++] = v
        if (pos == N) {
            pos = 0
            analyze()
        }
    }

    private fun analyze() {
        for (i in 0 until N) { re[i] = win[i] * hann[i]; im[i] = 0f }
        fft(re, im)
        val out = FloatArray(BANDS)
        for (k in 0 until BANDS) {
            var m = 0f
            val a = edges[k]
            val z = maxOf(a, edges[k + 1] - 1)
            for (j in a..z) m = maxOf(m, hypot(re[j], im[j]))
            val db = 20 * log10(m / (N / 4f) + 1e-9f)
            val tilt = 0.85f + 0.3f * k / BANDS             // puțin mai mult pe înalte
            out[k] = (((db + 72f) / 62f).coerceIn(0f, 1f)).let { Math.pow(it.toDouble(), 1.3).toFloat() * tilt }
        }
        synchronized(queue) {
            queue.addLast(Frame(System.nanoTime() + LATENCY_NS, out))
            while (queue.size > 200) queue.removeFirst()
        }
    }

    /** nivelurile care se aud acum, sau null dacă nu se redă nimic */
    fun current(): FloatArray? {
        val now = System.nanoTime()
        synchronized(queue) {
            var cur: Frame? = null
            while (queue.isNotEmpty() && queue.first().t <= now) cur = queue.removeFirst()
            if (cur != null) { last = cur; return cur.v }
            val l = last ?: return null
            return if (now - l.t < 300_000_000L) l.v else null
        }
    }

    private var last: Frame? = null

    private fun fft(x: FloatArray, y: FloatArray) {
        val n = x.size
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
            j = j xor bit
            if (i < j) {
                var t = x[i]; x[i] = x[j]; x[j] = t
                t = y[i]; y[i] = y[j]; y[j] = t
            }
        }
        var len = 2
        while (len <= n) {
            val ang = -2 * PI / len
            val wr = cos(ang).toFloat()
            val wi = kotlin.math.sin(ang).toFloat()
            var i = 0
            while (i < n) {
                var cr = 1f
                var ci = 0f
                for (k in 0 until len / 2) {
                    val a = i + k
                    val b = a + len / 2
                    val tr = x[b] * cr - y[b] * ci
                    val ti = x[b] * ci + y[b] * cr
                    x[b] = x[a] - tr; y[b] = y[a] - ti
                    x[a] += tr; y[a] += ti
                    val nr = cr * wr - ci * wi
                    ci = cr * wi + ci * wr
                    cr = nr
                }
                i += len
            }
            len = len shl 1
        }
    }
}
