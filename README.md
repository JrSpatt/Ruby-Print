# Ruby Print

MX10 termal yazıcı için Windows üzerinde çalışan yerel PDF yazdırma arayüzü.
TiMini Print CLI 0.9.1 kullanır.

## İndir ve çalıştır

1. Bu deponun **Releases** bölümünü açın.
2. **Ruby_Print_Windows.zip** dosyasını indirin ve tamamen ayıklayın.
3. **Baslat.bat** dosyasını çalıştırın.
4. Tarayıcıda PDF dosyasını seçin, sayfa aralığını belirleyin ve yazdırın.

Windows 10/11, Intel veya AMD 64 bit. Hazır pakette Python ve TiMini motoru
bulunur; ayrıca Python kurulumu gerekmez.

## Özellikler

- PDF içe aktarma, otomatik sayfa sayısı ve sayfa aralığı seçimi.
- Açılır kapanır PDF önizleme ve baskı ayarları.
- Floyd–Steinberg, Threshold, Atkinson ve Bayer seçenekleri.
- Yatay/dikey kenar boşluğu kırpma, koyuluk ve ek sayfa aralığı.
- İlerleme, duraklatma, durdurma ve indirilebilir işlem kaydı.

## Baskı davranışı

Her iş en fazla iki sayfa içerir. TiMini işlemi bittikten sonra sonraki işten
önce 3 saniye beklenir. Son tek sayfa ayrıca gönderilir.

Bluetooth bağlantı hatasında, işlem kaydı açıkça sıfır bayt gönderildiğini
bildiriyorsa 3 saniye arayla en fazla iki yeniden deneme yapılır. Baskı verisi
gönderilmeye başladıysa veya durum belirsizse otomatik tekrar yapılmaz.
Kâğıt hatalarında işlem durur.

İlerleme, CLI tarafından tamamlanan işleri gösterir. Fiziksel çıktının eksiksiz
olduğunu doğrulamaz. Duraklatma ve durdurma mevcut ikili tamamlandıktan sonra
uygulanır.

## Kaynaktan çalıştırma

Python 3.11 veya üzeri ve Windows Bluetooth desteği gerekir.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app\server.py
```

Başlatmadan önce TiMini Print v0.9.1 Windows x86_64 CLI dosyasını indirip
`engine/TiMini-Print-Command-Line-Windows-x86_64.exe` konumuna koyun.
Dosya hazır Windows paketinin `engine` klasöründen de alınabilir.

Motor sürümü: https://github.com/Dejniel/TiMini-Print/releases/tag/v0.9.1

## Testler

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Gizlilik

Sunucu yalnızca `127.0.0.1` üzerinde çalışır. PDF dosyaları internete gönderilmez;
yazdırma için geçici bir yerel kopya oluşturulur. Sunucu normal kapatıldığında
bu kopya silinir. Tarayıcı sekmesini kapatmak sunucuyu kapatmaz; uygulamadaki
kapatma düğmesini kullanın.

## Lisanslar

Ruby Print arayüzü ve sarmalayıcısı GNU GPL v3.0 (GPL-3.0-only) lisanslıdır.
Değiştirilmiş Ruby Print sürümlerini dağıtırken GPLv3 koşulları ve ilgili kaynak
kodu sağlama yükümlülükleri geçerlidir. TiMini Print değiştirilmeden
ayrı işlem olarak kullanılır ve Apache-2.0 lisanslıdır. Python, pypdf ve TiMini
bağımlılıklarının bildirimleri `licenses` klasöründedir. Bu proje TiMini'nin
resmî bir sürümü değildir. Önceki MIT dağıtımının bildirimi
`licenses/RUBY-PRINT-ORIGINAL-MIT.txt` dosyasında korunur.
