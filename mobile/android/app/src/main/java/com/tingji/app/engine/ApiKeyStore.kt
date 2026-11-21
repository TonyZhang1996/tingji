package com.tingji.app.engine

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import java.io.File
import java.security.KeyStore
import java.security.UnrecoverableKeyException
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Only authenticated ciphertext is stored; its wrapping key stays in Android Keystore. */
internal class ApiKeyStore(context:Context) {
    private val directory=File(context.noBackupFilesDir,"tingji-credentials")
    internal val credentialFile=File(directory,"deepseek-api-key.bin")
    internal val keyAlias="${context.packageName}.tingji.deepseek.v1.${sha256(context.noBackupFilesDir.canonicalPath).take(24)}"
    private val atomic=AtomicFile(credentialFile)
    private val associatedData="${context.packageName}:tingji-deepseek-api-key:v1".toByteArray(Charsets.UTF_8)

    @Synchronized fun hasSavedKey():Boolean = try { load()!=null } catch(_:SavedKeyUnavailable) { false }

    /** Missing and unreadable are distinct: damaged data must never become a request credential. */
    @Synchronized fun load():String? {
        if(!credentialFile.exists() && !File(credentialFile.path+".bak").exists())return null
        try {
            val data=atomic.openRead().use {input->
                val bytes=ByteArray(MAX_FILE_BYTES+1);var count=0
                while(count<bytes.size) {val n=input.read(bytes,count,bytes.size-count);if(n<0)break;count+=n}
                require(count in MAGIC.size+IV_BYTES+17..MAX_FILE_BYTES)
                bytes.copyOf(count)
            }
            require(data.copyOfRange(0,MAGIC.size).contentEquals(MAGIC))
            val key=keyStore().getKey(keyAlias,null) as? SecretKey ?: throw SavedKeyUnavailable()
            val cipher=Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE,key,GCMParameterSpec(128,data.copyOfRange(MAGIC.size,MAGIC.size+IV_BYTES)))
            cipher.updateAAD(associatedData)
            val plain=cipher.doFinal(data.copyOfRange(MAGIC.size+IV_BYTES,data.size))
            return try { validate(String(plain,Charsets.UTF_8)) } finally { plain.fill(0) }
        } catch(_:Exception) { throw SavedKeyUnavailable() }
    }

    @Synchronized fun save(raw:String) {
        val value=validate(raw)
        val plain=value.toByteArray(Charsets.UTF_8)
        try {
            require(directory.mkdirs() || directory.isDirectory)
            val cipher=Cipher.getInstance(TRANSFORMATION)
            try { cipher.init(Cipher.ENCRYPT_MODE,wrappingKey()) }
            catch(_:KeyPermanentlyInvalidatedException) {
                val store=keyStore();store.deleteEntry(keyAlias)
                cipher.init(Cipher.ENCRYPT_MODE,wrappingKey())
            }
            require(cipher.iv.size==IV_BYTES)
            cipher.updateAAD(associatedData)
            val encrypted=cipher.doFinal(plain)
            val output=atomic.startWrite()
            try {
                output.write(MAGIC);output.write(cipher.iv);output.write(encrypted)
                atomic.finishWrite(output)
            } catch(error:Exception) { atomic.failWrite(output);throw error }
            require(load()==value)
        } catch(_:Exception) {
            throw IllegalStateException("API Key 未能加密保存，请稍后重试；录音和文字未改动。")
        } finally { plain.fill(0) }
    }

    @Synchronized fun clear() {
        try {
            atomic.delete()
            require(listOf(credentialFile,File(credentialFile.path+".bak"),File(credentialFile.path+".new")).none{it.exists()})
        } catch(_:Exception) { throw IllegalStateException("本机保存的 API Key 未能清除，请稍后重试。") }
        // Ciphertext deletion removes the credential even if a provider cannot
        // immediately remove an unused wrapping-key entry.
        try { keyStore().deleteEntry(keyAlias) } catch(_:Exception) { }
    }

    private fun keyStore()=KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
    private fun wrappingKey():SecretKey {
        val store=keyStore()
        val existing=try {store.getKey(keyAlias,null) as? SecretKey}
            catch(_:UnrecoverableKeyException) {store.deleteEntry(keyAlias);null}
        if(existing!=null)return existing
        if(store.containsAlias(keyAlias))store.deleteEntry(keyAlias)
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(keyAlias,KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setKeySize(256).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).setUserAuthenticationRequired(false).build())
            generateKey()
        }
    }

    internal class SavedKeyUnavailable:IllegalStateException("本机保存的 API Key 无法读取，请到转写设置重新保存或清除后再填写。")
    companion object {
        private const val TRANSFORMATION="AES/GCM/NoPadding"
        private const val IV_BYTES=12
        private const val MAX_FILE_BYTES=2048
        private val MAGIC=byteArrayOf(0x54,0x4a,0x4b,0x01)
        fun validate(raw:String):String {
            require(raw.length<=512 && raw.none {it.isISOControl()}) {"API Key 格式不正确，请只粘贴 Key，不要包含换行或控制字符（最多512个字符）。"}
            val value=raw.trim()
            require(value.isNotEmpty()) {"请填写自己的 DeepSeek API Key。"}
            require(Regex("[A-Za-z0-9._~+/-]+={0,2}").matches(value)) {"API Key 格式不正确，请只粘贴 Key，不要包含空格、引号或其他内容。"}
            return value
        }
    }
}
