# GitHub・PyPI への公開

[English](../releasing.md) | 日本語

[README](../../README.ja.md) · [セットアップ](setup.md)

配布名は `rpi-cxd56xx-gnss`、Python でのインポート名は `cxd56xx_gnss` です。
利用者が Raspberry Pi 上で GPIO 設定を指定して C 拡張をビルドできるよう、ソース配布物（sdist）を公開します。
libgpiod などのシステム依存パッケージは、[セットアップ手順](setup.md)に従って別途インストールします。
PyPI のパッケージ説明にも英語版 README を使用します。

`main` への push とプルリクエストでは、**Build and publish** が sdist を作成してメタデータを検証し、
Debian Trixie、Python 3.13、libgpiod 2.x の環境で wheel のビルドとインポートを確認します。
GNSS デバイスは開きません。パッケージインデックスへのアップロードには、ワークフローの手動実行が必要です。

## バージョンの公開

1. `pyproject.toml` の `version` を設定し、コミットして push します。初期バージョンは `0.1.0` です。
2. GitHub Actions のビルドが成功したコミットに、パッケージのバージョンと一致するタグを付けます。

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. **Actions → Build and publish → Run workflow** を開き、タグ `v0.1.0` を選択し、`repository` に `testpypi` を指定します。
   ブランチからの手動実行は拒否されます。
4. TestPyPI の説明表示、インストール、実機での受信を確認します。
5. 同じタグから再度ワークフローを実行し、本番公開用に `pypi` を選択します。

アップロードされるのは `dist/*.tar.gz` だけです。CI で生成した wheel はインポート確認用で、公開しません。
既存の配布ファイルは上書きできないため、修正版を公開する場合は新しいバージョンとタグを使用してください。

## TestPyPI からのインストール確認

[セットアップ手順](setup.md)に従ってシステム依存パッケージ・配線・I2C 400 kHz 設定を用意し、
確認用の仮想環境を別に作成します。TestPyPI を利用する前に、通常の PyPI からビルド依存パッケージをインストールします。

```sh
python3 -m venv .venv-testpypi
. .venv-testpypi/bin/activate
python -m pip install --upgrade pip "setuptools>=77" "Cython>=3.0"
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --no-deps --no-build-isolation --no-cache-dir \
  rpi-cxd56xx-gnss==0.1.0
cxd56xx-gnss
```

GPIO チップとバージョンは、実機と公開したバージョンに合わせて変更してください。
本番公開後は、利用者の通常の仮想環境に次のコマンドでインストールできます。

```sh
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install --no-cache-dir rpi-cxd56xx-gnss
```
