import java.util.Properties
import org.gradle.api.tasks.Sync

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

val repoRoot: File = rootProject.projectDir.parentFile
val generatedDir: File = layout.buildDirectory.dir("generated").get().asFile
val stagedPythonDir: File = File(generatedDir, "python-src")
val stagedAssetsDir: File = File(generatedDir, "assets")
val frontendDistDir: File = File(repoRoot, "frontend/dist")
// Chaquopy / AGP 在 configuration 阶段可能检查 srcDir 是否存在；先建空目录。
stagedPythonDir.mkdirs()
stagedAssetsDir.mkdirs()

val versionProps = Properties().apply {
    File(projectDir.parentFile, "version.properties").inputStream().use { load(it) }
}
val appVersionName: String = versionProps.getProperty("versionName", "0.0.0")
val appVersionCode: Int = versionProps.getProperty("versionCode", "1").toInt()
val overrideTargetSdk: Int? = providers.gradleProperty("yikouTargetSdk").orNull?.toIntOrNull()

android {
    namespace = "com.yikou.lightfood"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.yikou.lightfood"
        minSdk = 26
        // M0 决策：先按 targetSdk 35 验证 nativeLibraryDir 执行；若真机 SIGSYS/拦截
        // 严重再按设计文档的停机点降级（-PyikouTargetSdk=28）。
        targetSdk = overrideTargetSdk ?: 35
        versionCode = appVersionCode
        versionName = appVersionName
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            abiFilters.add("arm64-v8a")
        }
    }

    sourceSets.getByName("main") {
        // 仅放薄引导层；仓库 app/ 由 Sync 任务复制到 build/generated/python-src。
        assets.srcDir(stagedAssetsDir)
    }

    signingConfigs {
        val storePath = providers.environmentVariable("YIKOU_KEYSTORE_FILE").orNull
        if (!storePath.isNullOrBlank()) {
            create("release") {
                storeFile = project.file(storePath)
                storePassword = providers.environmentVariable("YIKOU_KEYSTORE_PASSWORD").orNull
                keyAlias = providers.environmentVariable("YIKOU_KEY_ALIAS").orNull
                keyPassword = providers.environmentVariable("YIKOU_KEY_PASSWORD").orNull
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            // M0/M1 可行性构建允许落到 debug 签名；正式分发必须在 CI Secrets 配好
            // YIKOU_KEYSTORE_*，否则不同构建之间无法覆盖安装。
            signingConfig = signingConfigs.findByName("release")
                ?: signingConfigs.getByName("debug")
        }
        debug {
            // debug 也保留同一套运行时行为，方便 M0 真机对拍。
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        // MainActivity/AppUpdater 用 BuildConfig.VERSION_NAME 做应用内更新比较。
        buildConfig = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    packaging {
        jniLibs {
            // libproot.so / libkdocs_cli.so 必须原样解压到 nativeLibraryDir 才能执行。
            useLegacyPackaging = true
            keepDebugSymbols.add("**/*.so")
        }
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            // 与 requirements.txt 的约束对齐；cryptography 使用 Chaquopy 仓库的
            // cp313/android_24_arm64_v8a 预编译包。
            install("openpyxl==3.1.5")
            install("requests==2.34.2")
            install("cryptography==42.0.8")
        }
    }
    sourceSets.getByName("main") {
        // Kotlin DSL：srcDirs 的 getter 是 Set<File>、setter 是 setSrcDirs(Iterable)，
        // 不能写属性赋值；按 Chaquopy 文档显式调用 setSrcDirs。
        setSrcDirs(listOf("src/main/python", stagedPythonDir.absolutePath))
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.browser:browser:1.8.0")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
}

// 把仓库 app/ 复制成 Chaquopy 的第二个源码根，保持「源码只在仓库根」，
// 同时避免把 .venv / __pycache__ 等本机目录打进 APK。
val stagePythonSources = tasks.register<Sync>("stagePythonSources") {
    from(File(repoRoot, "app")) {
        into("app")
    }
    into(stagedPythonDir)
    exclude("**/__pycache__/**", "**/*.pyc", "**/*.pyo")
}

// 把 Vite singlefile 产物复制到 assets/dist；没有构建前端时直接失败，
// 不生成一个白屏 APK。
val stageFrontendDist = tasks.register<Sync>("stageFrontendDist") {
    doFirst {
        require(File(frontendDistDir, "index.html").isFile) {
            "未找到 ${frontendDistDir.resolve("index.html")}；先执行 " +
                "`cd frontend && pnpm install --frozen-lockfile && pnpm build`"
        }
    }
    from(frontendDistDir) {
        into("dist")
    }
    into(stagedAssetsDir)
    exclude("**/*.map")
}

// Chaquopy 在 preBuild 之后收集源码；把 Sync 挂到 preBuild 保证编译时目录已就绪。
tasks.matching { it.name == "preBuild" }.configureEach {
    dependsOn(stagePythonSources, stageFrontendDist)
}
// 显式挂到资产合并 / Python 打包任务，避免 Gradle 并行或任务图优化时 Sync
// 与真正的消费任务失去顺序依赖（表现为 APK 缺 dist/index.html 或业务模块）。
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }.configureEach {
    dependsOn(stageFrontendDist)
}
tasks.matching {
    it.name.contains("Python", ignoreCase = true) && it.name != "stagePythonSources"
}.configureEach {
    dependsOn(stagePythonSources)
}
