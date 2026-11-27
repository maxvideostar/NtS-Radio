package eu.netstreaming.radio

import android.content.Context
import android.graphics.BlurMaskFilter
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import androidx.media3.common.util.UnstableApi

/**
 * Barele egalizatorului desenate exact peste locul lor din jingle-logo-bg.png (coordonate pe 800x800).
 */
@UnstableApi
class BarsView @JvmOverloads constructor(ctx: Context, attrs: AttributeSet? = null) : View(ctx, attrs) {
    private val nb = Levels.BANDS
    private val bw = 12f
    private val pitch = 21.4f
    private val x0 = 234f
    private val bottom = 764f
    private val hMax = 74f
    private val hMin = 6f
    private val lv = FloatArray(nb)
    private val bar = Paint(Paint.ANTI_ALIAS_FLAG)
    // strălucire estompată ca în sigla originală (albastru 70,140,255 · alfa 110 · blur 6 px pe 800)
    private val glow = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0x6E468CFF }

    init {
        setLayerType(LAYER_TYPE_SOFTWARE, null)        // BlurMaskFilter cere randare software
    }
    private val r = RectF()
    private var shaderH = -1f
    var running = false
        set(v) { field = v; invalidate() }

    override fun onDraw(c: Canvas) {
        val k = width / 800f
        if (k <= 0f) return
        if (shaderH != k) {
            shaderH = k
            glow.maskFilter = BlurMaskFilter(6f * k, BlurMaskFilter.Blur.NORMAL)
            bar.shader = LinearGradient(0f, (bottom - hMax) * k, 0f, bottom * k,
                0xFF82B9FF.toInt(), 0xFF2D6ECD.toInt(), Shader.TileMode.CLAMP)
        }
        val v = if (running) Levels.current() else null
        for (i in 0 until nb) {
            val target = v?.get(i) ?: 0f
            lv[i] = if (target > lv[i]) lv[i] + (target - lv[i]) * 0.6f else lv[i] * 0.88f
        }
        for (i in 0 until nb) {                       // întâi strălucirea, apoi barele (ca în siglă)
            val h = (hMin + (hMax - hMin) * lv[i]) * k
            val x = (x0 + i * pitch - bw / 2) * k
            r.set(x, bottom * k - h, x + bw * k, bottom * k)
            c.drawRoundRect(r, 5 * k, 5 * k, glow)
        }
        for (i in 0 until nb) {
            val h = (hMin + (hMax - hMin) * lv[i]) * k
            val x = (x0 + i * pitch - bw / 2) * k
            r.set(x, bottom * k - h, x + bw * k, bottom * k)
            c.drawRoundRect(r, 5 * k, 5 * k, bar)
        }
        if (running) postInvalidateOnAnimation()
    }
}
